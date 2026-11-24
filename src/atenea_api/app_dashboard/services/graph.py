"""Read-only graph data for a Job tree and its batch-level pipeline."""

import hashlib
import json
from collections import defaultdict
from uuid import UUID

from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from app_dashboard.constants import batch_status_meta, job_status_meta, pipeline_label, stage_label
from app_dashboard.enums import StageDisplayState
from app_dashboard.services.presentation import (
    aggregate_stage_summaries,
    present_batch,
    present_job,
    result_metrics_for_batches,
)
from app_scheduler.models import ProcessingBatch, ProcessingHandoff, ProcessingJob
from app_scheduler.enums import BatchStatus

MAX_INDIVIDUAL_PENDING_HANDOFFS = 25
VIRTUAL_PIPELINE_THRESHOLD = 150
VIRTUAL_EDGE_THRESHOLD = 500


def _revision(value):
    return hashlib.sha256(_serialize_detail(value).encode("utf-8")).hexdigest()


def _compact_pipeline_task(batch, job):
    """Keep graph rows small; render detailed cards only near the viewport."""

    workflow = job.workflow if isinstance(job.workflow, list) else []
    state = _pipeline_task_state(batch)
    stages = [
        {
            "key": stage,
            "label": stage_label(stage),
            "status": _stage_state_meta(_batch_stage_state(batch, stage, workflow)),
            "attempts": _attempt_count(batch, stage),
        }
        for stage in workflow
    ]
    task = {
        "id": f"batch-{batch.pk}",
        "node_id": str(batch.pk),
        "job_id": str(job.pk),
        "label": _("Batch %(ordinal)s") % {"ordinal": batch.ordinal},
        "ordinal": batch.ordinal,
        "state": state,
        "status": {**PIPELINE_TASK_STATUS[state], "icon": batch_status_meta(batch.status)["icon"]},
        "stage_summary": stages,
        "summary": _("Processed %(processed)s · Failed %(failed)s") % {
            "processed": batch.processed_count, "failed": batch.failed_count,
        },
        "last_error": batch.last_error[:132],
        "placeholder": False,
        "dependencies": [],
        "detail": None,
        "detail_json": "",
        "_changed_at": batch.updated_at,
    }
    task["revision"] = _revision([
        task["state"], stages, task["summary"], batch.last_error,
        batch.updated_at.isoformat(), job.status, batch.result_summary,
    ])
    return task


def collect_job_tree(root):
    jobs = {root.pk: root}
    frontier = [root.pk]
    while frontier:
        children = list(
            ProcessingJob.objects.filter(parent_job_id__in=frontier).order_by("created_at")
        )
        frontier = [job.pk for job in children]
        jobs.update({job.pk: job for job in children})
    return sorted(jobs.values(), key=lambda job: (job.created_at, str(job.pk)))


def _group_batches_by_job(jobs, batches):
    """Reuse the already loaded Job rows while grouping their Batches."""

    jobs_by_id = {job.pk: job for job in jobs}
    batches_by_job = defaultdict(list)
    for batch in batches:
        batch.job = jobs_by_id[batch.job_id]
        batches_by_job[batch.job_id].append(batch)
    return batches_by_job


def _attempt_count(batch, stage):
    value = (batch.stage_attempts or {}).get(stage, 0)
    if isinstance(value, dict):
        value = value.get("attempt", value.get("count", 0))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _stage_summary(stage, batches):
    completed = sum(stage in (batch.completed_stages or []) for batch in batches)
    running = sum(
        batch.current_stage == stage and batch.status == ProcessingBatch.RUNNING
        for batch in batches
    )
    retry = sum(
        batch.current_stage == stage and batch.status == ProcessingBatch.RETRY_WAIT
        for batch in batches
    )
    failed = sum(
        batch.current_stage == stage and batch.status == ProcessingBatch.FAILED
        for batch in batches
    )
    total = len(batches)
    pending = max(total - completed - running - retry - failed, 0)
    attempts = sum(_attempt_count(batch, stage) for batch in batches)

    if failed:
        state = StageDisplayState.FAILED.value
    elif retry:
        state = StageDisplayState.RETRY.value
    elif running:
        state = StageDisplayState.RUNNING.value
    elif total and completed >= total:
        state = StageDisplayState.COMPLETED.value
    else:
        state = StageDisplayState.PENDING.value

    state_meta = {
        StageDisplayState.COMPLETED: {"label": _("Completed"), "tone": "success", "icon": "✓", "animated": False},
        StageDisplayState.RUNNING: {"label": _("Running"), "tone": "running", "icon": "▶", "animated": True},
        StageDisplayState.RETRY: {"label": _("Retrying"), "tone": "retry", "icon": "↻", "animated": False},
        StageDisplayState.FAILED: {"label": _("Failed"), "tone": "error", "icon": "!", "animated": False},
        StageDisplayState.PENDING: {"label": _("Pending"), "tone": "muted", "icon": "○", "animated": False},
    }[state]
    return {
        "key": stage,
        "label": stage_label(stage),
        "state": state,
        "status": state_meta,
        "total": total,
        "completed": completed,
        "running": running,
        "retry": retry,
        "failed": failed,
        "pending": pending,
        "attempts": attempts,
    }


def _stage_state_meta(state):
    return {
        StageDisplayState.COMPLETED: {"label": _("Completed"), "tone": "success", "icon": "✓", "animated": False},
        StageDisplayState.RUNNING: {"label": _("Running"), "tone": "running", "icon": "▶", "animated": True},
        StageDisplayState.RETRY: {"label": _("Retrying"), "tone": "retry", "icon": "↻", "animated": False},
        StageDisplayState.FAILED: {"label": _("Failed"), "tone": "error", "icon": "!", "animated": False},
        StageDisplayState.CANCELLED: {"label": _("Cancelled"), "tone": "muted", "icon": "×", "animated": False},
        StageDisplayState.PENDING: {"label": _("Pending"), "tone": "muted", "icon": "○", "animated": False},
    }[state]


def _batch_stage_state(batch, stage, workflow):
    completed_stages = batch.completed_stages or []
    if stage in completed_stages:
        return StageDisplayState.COMPLETED.value
    if batch.current_stage == stage:
        return {
            BatchStatus.RUNNING: StageDisplayState.RUNNING.value,
            BatchStatus.RETRY_WAIT: StageDisplayState.RETRY.value,
            BatchStatus.FAILED: StageDisplayState.FAILED.value,
            BatchStatus.CANCELLED: StageDisplayState.CANCELLED.value,
        }.get(batch.status, StageDisplayState.PENDING.value)
    if batch.status == BatchStatus.CANCELLED and stage not in completed_stages:
        return StageDisplayState.CANCELLED.value
    # A failed batch may have been persisted without a current_stage by an
    # older worker.  Mark the first incomplete stage as the failed one so the
    # visual explanation remains useful instead of showing a false success.
    if batch.status == BatchStatus.FAILED and not batch.current_stage:
        incomplete = next((item for item in workflow if item not in completed_stages), None)
        if stage == incomplete:
            return StageDisplayState.FAILED.value
    return StageDisplayState.PENDING.value


def _serialize_batch(batch, workflow):
    presented = present_batch(batch)
    stages = []
    for stage in workflow:
        state = _batch_stage_state(batch, stage, workflow)
        stages.append(
            {
                "key": stage,
                "label": stage_label(stage),
                "state": state,
                "status": _stage_state_meta(state),
                "attempts": _attempt_count(batch, stage),
                "current": batch.current_stage == stage,
            }
        )
    return {
        "batch_id": str(batch.pk),
        "ordinal": batch.ordinal,
        "stage_results": presented["stage_results"],
        "input_label": presented["input_label"],
        "input_explanation": presented["input_explanation"],
        "skipped_count": presented["skipped_count"],
        "result_metrics": presented["result_metrics"],
        "count_breakdown": presented["count_breakdown"],
        "count_explanation": presented["count_explanation"],
        "status": batch_status_meta(batch.status),
        "status_code": batch.status,
        "item_count": batch.item_count,
        "processed_count": batch.processed_count,
        "failed_count": batch.failed_count,
        "current_stage": presented["current_stage"],
        "stages": stages,
        "last_error": batch.last_error,
        "updated_at": batch.updated_at,
    }


PIPELINE_TASK_STATUS = {
    StageDisplayState.QUEUED: {"label": _("Queued"), "tone": "muted", "animated": False},
    StageDisplayState.RETRYING: {"label": _("Retrying"), "tone": "retry", "animated": False},
    StageDisplayState.RUNNING: {"label": _("Running"), "tone": "running", "animated": True},
    StageDisplayState.COMPLETED: {"label": _("Completed"), "tone": "success", "animated": False},
    StageDisplayState.FAILED: {"label": _("Failed"), "tone": "error", "animated": False},
    StageDisplayState.CANCELLED: {"label": _("Cancelled"), "tone": "muted", "animated": False},
}


def _pipeline_task_state(batch):
    return {
        BatchStatus.PENDING: StageDisplayState.QUEUED.value,
        BatchStatus.RETRY_WAIT: StageDisplayState.RETRYING.value,
        BatchStatus.RUNNING: StageDisplayState.RUNNING.value,
        BatchStatus.COMPLETED: StageDisplayState.COMPLETED.value,
        BatchStatus.FAILED: StageDisplayState.FAILED.value,
        BatchStatus.CANCELLED: StageDisplayState.CANCELLED.value,
    }.get(batch.status, StageDisplayState.QUEUED.value)


def _json_safe(value):
    """Convert translated proxy values inside presentation data for JSON."""

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _serialize_detail(detail):
    return json.dumps(_json_safe(detail), ensure_ascii=False, separators=(",", ":"))


def _pipeline_task(batch, job, *, compact=False):
    compact_task = _compact_pipeline_task(batch, job)
    if compact:
        return compact_task
    presented = present_batch(batch)
    batch_state = _pipeline_task_state(batch)
    workflow = job.workflow if isinstance(job.workflow, list) else []
    stage_results = presented["stage_results"]
    workflow_states = {
        stage_key: _batch_stage_state(batch, stage_key, workflow)
        for stage_key in workflow
    }
    for stage in stage_results:
        stage_state = workflow_states.get(stage["key"], stage.get("state", StageDisplayState.PENDING.value))
        stage["state"] = stage_state
        stage["status"] = _stage_state_meta(stage_state)

    detail = {
        "batch_id": str(batch.pk),
        "ordinal": batch.ordinal,
        "job_id": str(job.pk),
        "job_label": pipeline_label(job.processor),
        "job_status": job_status_meta(job.status)["label"],
        "state": batch_state,
        "batch_status": batch_status_meta(batch.status)["label"],
        "current_stage": presented["current_stage"],
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        "updated_at": batch.updated_at.isoformat() if batch.updated_at else None,
        "counts": presented["count_breakdown"],
        "dependencies": [],
        "stages": stage_results,
        "last_error": batch.last_error,
    }

    task = {
        **compact_task,
        "id": f"batch-{batch.pk}",
        "node_id": str(batch.pk),
        "job_id": str(job.pk),
        "job_label": pipeline_label(job.processor),
        "label": _("Batch %(ordinal)s") % {"ordinal": batch.ordinal},
        "state": batch_state,
        "status": compact_task["status"],
        "ordinal": batch.ordinal,
        "item_count": presented["item_count"],
        "processed_count": presented["processed_count"],
        "failed_count": presented["failed_count"],
        "stage_results": stage_results,
        "input_label": presented["input_label"],
        "input_explanation": presented["input_explanation"],
        "skipped_count": presented["skipped_count"],
        "result_metrics": presented["result_metrics"],
        "count_breakdown": presented["count_breakdown"],
        "count_explanation": presented["count_explanation"],
        "current_stage": presented["current_stage"],
        "last_error": batch.last_error,
        "stages": stage_results,
        "dependencies": [],
        "detail": detail,
        "placeholder": False,
    }
    task["detail_json"] = _serialize_detail(detail)
    return task


def _pipeline_waiting_task(
    job, handoff_id=None, *, handoff_count=0, item_count=0, item_label=None,
):
    waiting_id = handoff_id or job.pk
    return {
        "id": f"waiting-{waiting_id}",
        "node_id": str(waiting_id),
        "job_id": str(job.pk),
        "job_label": pipeline_label(job.processor),
        "label": _("Waiting for input"),
        "state": StageDisplayState.QUEUED.value,
        "status": PIPELINE_TASK_STATUS[StageDisplayState.QUEUED],
        "ordinal": None,
        "handoff_count": handoff_count,
        "item_count": item_count,
        "item_label": item_label or str(_("items")),
        "waiting_message": (
            _("This Job is paused. Resume it to process the pending input.")
            if job.status == ProcessingJob.PAUSED and handoff_count
            else ""
        ),
        "skipped_count": 0,
        "result_metrics": [],
        "count_breakdown": [],
        "count_explanation": "",
        "processed_count": 0,
        "failed_count": 0,
        "current_stage": _("Not started"),
        "last_error": "",
        "stages": [],
        "dependencies": [],
        "detail": None,
        "detail_json": "",
        "placeholder": True,
    }


def _job_depths(jobs, root):
    """Return a stable left-to-right rank for every Job in the tree."""

    depths = {root.pk: 0}
    remaining = [job for job in jobs if job.pk != root.pk]
    while remaining:
        unresolved = []
        progress = False
        for job in remaining:
            if job.parent_job_id in depths:
                depths[job.pk] = depths[job.parent_job_id] + 1
                progress = True
            else:
                unresolved.append(job)
        if not progress:
            # A malformed/orphaned tree should still render instead of
            # preventing the operator from inspecting the rest of the Job.
            for job in unresolved:
                depths[job.pk] = 1
            break
        remaining = unresolved
    return depths


def _build_pipeline(jobs, batches_by_job, handoffs, root, *, compact=False, full_task_ids=()):
    virtual = sum(len(batches) for batches in batches_by_job.values()) > VIRTUAL_PIPELINE_THRESHOLD
    full_task_ids = set(full_task_ids)
    depths = _job_depths(jobs, root)
    jobs_by_depth = defaultdict(list)
    tasks_by_id = {}
    pending_handoffs_by_job = defaultdict(list)
    for handoff in handoffs:
        if handoff.status == ProcessingHandoff.PENDING and handoff.target_batch_id is None:
            pending_handoffs_by_job[handoff.target_job_id].append(handoff)

    aggregated_pending_jobs = set()
    for job in jobs:
        batches = batches_by_job.get(job.pk, [])
        tasks = [
            _pipeline_task(batch, job, compact=(compact or virtual) and batch.pk not in full_task_ids)
            for batch in batches
        ]
        pending_handoffs = pending_handoffs_by_job.get(job.pk, [])
        if len(pending_handoffs) > MAX_INDIVIDUAL_PENDING_HANDOFFS:
            aggregated_pending_jobs.add(job.pk)
            item_type = pending_handoffs[0].item_content_type
            model = item_type.model_class() if item_type else None
            item_label = str(model._meta.verbose_name_plural) if model else str(_("items"))
            tasks.append(
                _pipeline_waiting_task(
                    job,
                    f"pending-{job.pk}",
                    handoff_count=len(pending_handoffs),
                    item_count=sum(handoff.item_count for handoff in pending_handoffs),
                    item_label=item_label,
                )
            )
        else:
            tasks.extend(
                _pipeline_waiting_task(
                    job,
                    handoff.pk,
                    handoff_count=1,
                    item_count=handoff.item_count,
                    item_label=(
                        str(handoff.item_content_type.model_class()._meta.verbose_name_plural)
                        if handoff.item_content_type_id and handoff.item_content_type.model_class()
                        else str(_("items"))
                    ),
                )
                for handoff in pending_handoffs
            )
        if not tasks:
            tasks = [_pipeline_waiting_task(job)]
        for task in tasks:
            task.setdefault("_changed_at", job.updated_at)
            tasks_by_id[task["id"]] = task
        jobs_by_depth[depths[job.pk]].append(
            {
                "id": f"job-{job.pk}",
                "job_id": str(job.pk),
                "label": pipeline_label(job.processor),
                "status": job_status_meta(job.status),
                "status_code": job.status,
                "batch_count": len(batches),
                "handoffs": sum(handoff.target_job_id == job.pk for handoff in handoffs),
                "tasks": tasks,
            }
        )

    edges = []
    seen_edges = set()
    edges_by_key = {}
    for handoff in handoffs:
        if handoff.status == ProcessingHandoff.CANCELLED:
            continue
        source_id = f"batch-{handoff.source_batch_id}"
        aggregate_pending = (
            handoff.target_batch_id is None
            and handoff.status == ProcessingHandoff.PENDING
            and handoff.target_job_id in aggregated_pending_jobs
        )
        target_id = f"batch-{handoff.target_batch_id}" if handoff.target_batch_id else (
            f"waiting-pending-{handoff.target_job_id}"
            if aggregate_pending else f"waiting-{handoff.pk}"
        )
        if source_id not in tasks_by_id or target_id not in tasks_by_id:
            continue
        edge_key = (source_id, target_id)
        aggregate_edge = aggregate_pending or handoff.target_batch_id is not None
        if edge_key in seen_edges:
            edge = edges_by_key[edge_key]
            edge["transfer_count"] += handoff.item_count
            edge["handoff_count"] = edge.get("handoff_count", 1) + 1
            detail = tasks_by_id[target_id].get("detail")
            if detail is not None:
                dependency_id = tasks_by_id[source_id].get("node_id", source_id)
                for dependency in detail["dependencies"]:
                    if dependency["id"] == dependency_id:
                        dependency["transfer_count"] += handoff.item_count
                        dependency["handoff_count"] = dependency.get("handoff_count", 1) + 1
                        break
            continue
        seen_edges.add(edge_key)
        target_task = tasks_by_id[target_id]
        source_task = tasks_by_id[source_id]
        target_task["_changed_at"] = max(
            target_task["_changed_at"], source_task["_changed_at"], handoff.updated_at,
        )
        if source_id not in target_task["dependencies"]:
            target_task["dependencies"].append(source_id)
        content_model = handoff.item_content_type.model_class() if handoff.item_content_type_id else None
        transfer_unit = str(content_model._meta.verbose_name_plural) if content_model else str(_("items"))
        if target_task.get("detail") is not None:
            target_task["detail"]["dependencies"].append(
                {
                    "id": source_task.get("node_id", source_id),
                    "label": source_task["label"],
                    "job_label": source_task.get("job_label", ""),
                    "job_id": source_task.get("job_id", ""),
                    "status": source_task["status"]["label"],
                    "transfer_count": handoff.item_count,
                    "transfer_unit": transfer_unit,
                    "transfer_status": handoff.get_status_display(),
                }
            )
        edge = {
            "id": (
                f"edge-pending-{handoff.target_job_id}-{handoff.source_batch_id}"
                if aggregate_pending else f"edge-{handoff.pk}"
            ),
            "source": source_id,
            "target": target_id,
            "state": target_task["state"],
            "transfer_count": handoff.item_count,
            "transfer_unit": transfer_unit,
            "transfer_status": handoff.get_status_display(),
        }
        if aggregate_edge:
            edge["handoff_count"] = 1
        edges.append(edge)
        edges_by_key[edge_key] = edge

    # Serialize a batch's dependencies once, after collecting all handoffs.
    for task in tasks_by_id.values():
        if task["placeholder"]:
            task["revision"] = _task_snapshot(task)["revision"]
    base_revisions = {key: task.get("revision", "") for key, task in tasks_by_id.items()}
    incoming_revisions = defaultdict(list)
    for edge in edges:
        incoming_revisions[edge["target"]].append([base_revisions[edge["source"]], edge])
    for task in tasks_by_id.values():
        if incoming_revisions[task["id"]]:
            task["revision"] = _revision([task.get("revision", ""), incoming_revisions[task["id"]]])
        if task.get("detail") is not None:
            task["detail_json"] = _serialize_detail(task["detail"])

    columns = []
    for depth in sorted(jobs_by_depth):
        rank_jobs = jobs_by_depth[depth]
        columns.append(
            {
                "id": f"rank-{depth}",
                "label": _("Root Job") if depth == 0 else _("Pipeline stage %(number)s") % {"number": depth + 1},
                "jobs": rank_jobs,
                "task_count": sum(len(job["tasks"]) for job in rank_jobs),
            }
        )
    pipeline = {
        "columns": columns,
        "edges": edges,
        "task_count": len(tasks_by_id),
        "virtual": virtual or len(edges) > VIRTUAL_EDGE_THRESHOLD,
    }
    pipeline["snapshot"] = _pipeline_snapshot(pipeline)
    return pipeline


def _task_snapshot(task):
    fields = ("id", "node_id", "job_id", "label", "ordinal", "state", "status", "placeholder", "last_error")
    row = {key: task.get(key) for key in fields}
    row["last_error"] = (row["last_error"] or "")[:132]
    row["stages"] = task.get("stage_summary", [])
    if task["placeholder"]:
        count = task.get("handoff_count", 0)
        row["summary"] = (
            ngettext(
                "%(count)s pending handoff · %(items)s %(unit)s waiting",
                "%(count)s pending handoffs · %(items)s %(unit)s waiting", count,
            ) % {"count": count, "items": task["item_count"], "unit": task["item_label"]}
            if count else _("No materialized batches yet")
        )
        row["waiting_message"] = task.get("waiting_message", "")
    else:
        row["summary"] = task["summary"]
    row["revision"] = task.get("revision") or _revision(row)
    return _json_safe(row)


def _pipeline_snapshot(pipeline):
    columns = []
    for column in pipeline["columns"]:
        jobs = []
        for job in column["jobs"]:
            jobs.append({
                **{key: job[key] for key in ("job_id", "label", "status", "status_code", "batch_count")},
                "batch_count_label": ngettext("%(count)s Batch", "%(count)s Batches", job["batch_count"]) % {"count": job["batch_count"]},
                "tasks": [_task_snapshot(task) for task in job["tasks"]],
            })
        columns.append({
            "id": column["id"], "jobs": jobs, "task_count": column["task_count"],
            "task_count_label": ngettext("%(count)s batch", "%(count)s batches", column["task_count"]) % {"count": column["task_count"]},
        })
    snapshot = _json_safe({
        "columns": columns, "edges": pipeline["edges"], "virtual": pipeline["virtual"],
        "task_count_label": ngettext("%(count)s batch node", "%(count)s batch nodes", pipeline["task_count"]) % {"count": pipeline["task_count"]},
    })
    # Edge state follows the target row, so status changes do not change topology.
    topology = [
        pipeline["virtual"],
        [
            [column["id"], [
                [job["job_id"], [task["id"] for task in job["tasks"]]]
                for job in column["jobs"]
            ]]
            for column in columns
        ],
        [{key: value for key, value in edge.items() if key != "state"} for edge in snapshot["edges"]],
    ]
    snapshot["topology"] = _revision(topology)
    snapshot["revision"] = _revision(snapshot)
    return snapshot


def _workflow_summary(batches):
    """Return per-stage totals without merging identical metrics across stages."""

    summaries = aggregate_stage_summaries(batches)
    for stage in summaries:
        stage_batches = [
            batch
            for batch in batches
            if stage["key"] in (batch.job.workflow if isinstance(batch.job.workflow, list) else [])
        ]
        states = [
            _batch_stage_state(
                batch,
                stage["key"],
                batch.job.workflow if isinstance(batch.job.workflow, list) else [],
            )
            for batch in stage_batches
        ]
        counts = {state: states.count(state) for state in set(states)}
        if counts.get(StageDisplayState.FAILED):
            state = StageDisplayState.FAILED.value
        elif counts.get(StageDisplayState.RETRY):
            state = StageDisplayState.RETRY.value
        elif counts.get(StageDisplayState.RUNNING):
            state = StageDisplayState.RUNNING.value
        elif counts.get(StageDisplayState.PENDING):
            state = StageDisplayState.PENDING.value
        elif counts.get(StageDisplayState.CANCELLED):
            state = StageDisplayState.CANCELLED.value
        elif counts.get(StageDisplayState.COMPLETED):
            state = StageDisplayState.COMPLETED.value
        else:
            state = StageDisplayState.PENDING.value
        stage.update(
            {
                "state": state,
                "status": _stage_state_meta(state),
                "total": len(stage_batches),
                "completed": counts.get(StageDisplayState.COMPLETED, 0),
                "running": counts.get(StageDisplayState.RUNNING, 0),
                "retry": counts.get(StageDisplayState.RETRY, 0),
                "failed_count": counts.get(StageDisplayState.FAILED, 0),
                "pending": counts.get(StageDisplayState.PENDING, 0),
            }
        )
    return summaries


def _processing_summary(jobs):
    children = jobs[1:]
    return {
        "pending_child_jobs": sum(job.status in ProcessingJob.ACTIVE_STATUSES for job in children),
        "retryable_child_jobs": sum(job.can_retry_failed for job in children),
    }


def build_job_live_summary(root):
    """Build the live summary without serializing the full pipeline graph."""

    jobs = collect_job_tree(root)
    job_ids = [job.pk for job in jobs]
    batches = list(
        ProcessingBatch.objects.filter(job_id__in=job_ids)
        .select_related("item_content_type")
        .defer("item_ids", "processor_metadata")
        .order_by("job_id", "ordinal")
    )
    batches_by_job = _group_batches_by_job(jobs, batches)
    depths = _job_depths(jobs, root)
    workflow_batches = [
        batch
        for job in sorted(jobs, key=lambda item: depths[item.pk])
        for batch in batches_by_job.get(job.pk, [])
    ]
    return {
        **_processing_summary(jobs),
        "job": present_job(root, batches=batches_by_job.get(root.pk, [])),
        "workflow_summary": _workflow_summary(workflow_batches),
        "active": any(job.status in ProcessingJob.ACTIVE_STATUSES for job in jobs),
    }


def _serialize_job(job, batches_by_job, children_by_job, handoffs_by_job):
    batches = batches_by_job.get(job.pk, [])
    workflow = job.workflow if isinstance(job.workflow, list) else []
    stages = [_stage_summary(stage, batches) for stage in workflow]
    item = present_job(job, batches=batches)
    item.update(
        {
            "job_id": str(job.pk),
            "job_label": pipeline_label(job.processor),
            "job_status": job_status_meta(job.status),
            "stages": stages,
            "batches": [_serialize_batch(batch, workflow) for batch in batches],
            "batch_count": len(batches),
            "handoffs": handoffs_by_job.get(job.pk, 0),
            "children": [
                _serialize_job(child, batches_by_job, children_by_job, handoffs_by_job)
                for child in children_by_job.get(job.pk, [])
            ],
        }
    )
    return item


def _load_pipeline_records(root):
    jobs = collect_job_tree(root)
    job_ids = [job.pk for job in jobs]
    batches = list(
        ProcessingBatch.objects.filter(job_id__in=job_ids)
        .select_related("item_content_type")
        .defer("item_ids", "processor_metadata")
        .order_by("job_id", "ordinal")
    )
    batches_by_job = _group_batches_by_job(jobs, batches)
    handoffs = list(ProcessingHandoff.objects.filter(
        Q(source_batch__job_id__in=job_ids) | Q(target_job_id__in=job_ids)
    ).select_related("item_content_type").defer("item_ids"))
    return jobs, batches_by_job, handoffs


def build_job_pipeline(root):
    """Load only the records needed to draw a Job tree's pipeline graph."""

    cursor = timezone.now().isoformat()
    jobs, batches_by_job, handoffs = _load_pipeline_records(root)
    pipeline = _build_pipeline(jobs, batches_by_job, handoffs, root)
    pipeline["snapshot"]["cursor"] = cursor
    return {
        "root_job_id": str(root.pk),
        "pipeline": pipeline,
        "active": any(job.status in ProcessingJob.ACTIVE_STATUSES for job in jobs),
    }


def build_job_pipeline_updates(root, *, since=None, topology=None, revision=None, job_states=None):
    """Send changed rows; render metrics only for changed rows in small graphs."""

    cursor = timezone.now().isoformat()
    jobs, batches_by_job, handoffs = _load_pipeline_records(root)
    pipeline = _build_pipeline(jobs, batches_by_job, handoffs, root, compact=True)
    snapshot = pipeline["snapshot"]
    response = {
        "cursor": cursor, "revision": snapshot["revision"], "topology": snapshot["topology"],
        "active": any(job.status in ProcessingJob.ACTIVE_STATUSES for job in jobs),
    }
    if revision == snapshot["revision"]:
        return {**response, "unchanged": True}
    if topology != snapshot["topology"]:
        return {**response, "structure": {**snapshot, "cursor": cursor}}

    tasks = [task for column in pipeline["columns"] for job in column["jobs"] for task in job["tasks"]]
    current_states = {str(job.pk): job.status for job in jobs}
    changed_jobs = {
        job_id for job_id, status in current_states.items()
        if job_states is not None and job_states.get(job_id) != status
    }
    changed = [task for task in tasks if since is None or task["_changed_at"] >= since or task["job_id"] in changed_jobs]
    # Also handle older writers which updated values without touching updated_at.
    if not changed:
        changed = tasks
    updates = [_task_snapshot(task) for task in changed]
    if not pipeline["virtual"]:
        full_ids = [task["node_id"] for task in changed if not task["placeholder"]]
        # Batch primary keys are UUIDs; normalize them before checking membership.
        detailed = _build_pipeline(
            jobs, batches_by_job, handoffs, root, compact=True,
            full_task_ids=[UUID(value) for value in full_ids],
        )
        detailed_tasks = {task["id"]: task for column in detailed["columns"] for job in column["jobs"] for task in job["tasks"]}
        for update in updates:
            update["html"] = render_to_string("app_dashboard/jobs/_graph_task.html", {"task": detailed_tasks[update["id"]]})
    response["updates"] = updates
    response["jobs"] = [
        {key: job[key] for key in ("job_id", "label", "status", "status_code", "batch_count", "batch_count_label")}
        for column in snapshot["columns"] for job in column["jobs"]
    ]
    return _json_safe(response)


def _load_batch_tasks(root, batch_ids):
    """Present only the requested batches, including their incoming transfers."""
    job_ids = [job.pk for job in collect_job_tree(root)]
    batches = list(ProcessingBatch.objects.select_related("job", "item_content_type").defer(
        "item_ids", "processor_metadata",
    ).filter(pk__in=batch_ids, job_id__in=job_ids))
    if len(batches) != len(set(batch_ids)):
        raise ProcessingBatch.DoesNotExist
    tasks = {batch.pk: _pipeline_task(batch, batch.job) for batch in batches}
    handoffs = ProcessingHandoff.objects.filter(target_batch_id__in=tasks).exclude(
        status=ProcessingHandoff.CANCELLED,
    ).select_related("source_batch__job", "item_content_type").defer(
        "item_ids", "source_batch__item_ids", "source_batch__processor_metadata",
    )
    seen = defaultdict(set)
    for handoff in handoffs:
        source = handoff.source_batch
        if source.pk in seen[handoff.target_batch_id]:
            continue
        seen[handoff.target_batch_id].add(source.pk)
        model = handoff.item_content_type.model_class()
        tasks[handoff.target_batch_id]["detail"]["dependencies"].append({
            "id": str(source.pk),
            "label": _("Batch %(ordinal)s") % {"ordinal": source.ordinal},
            "job_label": pipeline_label(source.job.processor),
            "job_id": str(source.job_id),
            "status": PIPELINE_TASK_STATUS[_pipeline_task_state(source)]["label"],
            "transfer_count": handoff.item_count,
            "transfer_unit": str(model._meta.verbose_name_plural) if model else str(_("items")),
            "transfer_status": handoff.get_status_display(),
        })
    for task in tasks.values():
        task["detail_json"] = _serialize_detail(task["detail"])
    return tasks


def build_batch_detail(root, batch_id):
    """Fetch one batch and its incoming dependencies, scoped to this Job tree."""
    return _json_safe(_load_batch_tasks(root, [batch_id])[batch_id]["detail"])


def build_batch_cards(root, batch_ids):
    """Reuse the original batch visual for the visible window of a large graph."""
    tasks = _load_batch_tasks(root, batch_ids)
    return {"cards": [
        {
            "id": tasks[batch_id]["id"],
            "html": render_to_string("app_dashboard/jobs/_graph_task.html", {"task": tasks[batch_id]}),
        }
        for batch_id in batch_ids
    ]}


def build_job_graph(root, *, include_job_details=True):
    """Build the complete detail-page graph, including job-level summaries."""

    cursor = timezone.now().isoformat()
    jobs = collect_job_tree(root)
    job_ids = [job.pk for job in jobs]
    batches = list(
        ProcessingBatch.objects.filter(job_id__in=job_ids)
        .select_related("item_content_type")
        .defer("item_ids", "processor_metadata")
        .order_by("job_id", "ordinal")
    )
    batches_by_job = _group_batches_by_job(jobs, batches)

    children_by_job = defaultdict(list)
    for job in jobs:
        if job.parent_job_id in job_ids:
            children_by_job[job.parent_job_id].append(job)
    handoffs = list(ProcessingHandoff.objects.filter(
        Q(source_batch__job_id__in=job_ids) | Q(target_job_id__in=job_ids)
    ).select_related("item_content_type").defer("item_ids"))
    handoffs_by_job = defaultdict(int)
    for handoff in handoffs:
        handoffs_by_job[handoff.target_job_id] += 1

    # Follow the DAG ranks and each Job's workflow, rather than UUID order.
    depths = _job_depths(jobs, root)
    workflow_batches = [
        batch
        for job in sorted(jobs, key=lambda job: depths[job.pk])
        for batch in batches_by_job.get(job.pk, [])
    ]

    pipeline = _build_pipeline(jobs, batches_by_job, handoffs, root)
    pipeline["snapshot"]["cursor"] = cursor
    return {
        **_processing_summary(jobs),
        "root_job_id": str(root.pk),
        "root": (
            _serialize_job(root, batches_by_job, children_by_job, handoffs_by_job)
            if include_job_details
            else present_job(root, batches=batches_by_job.get(root.pk, []))
        ),
        "pipeline": pipeline,
        "workflow_summary": _workflow_summary(workflow_batches),
        "pipeline_metrics": result_metrics_for_batches(batches),
        "jobs": jobs,
        "accessible_jobs": [
            {
                "label": pipeline_label(job.processor),
                "status": job_status_meta(job.status),
                "stages": [
                    {"label": stage_label(stage)}
                    for stage in (job.workflow if isinstance(job.workflow, list) else [])
                ],
            }
            for job in jobs
        ],
        "active": any(job.status in ProcessingJob.ACTIVE_STATUSES for job in jobs),
    }
