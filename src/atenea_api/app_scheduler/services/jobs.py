"""Generic durable-job lifecycle and lease protocol.

This module is intentionally ignorant of MessageItem, NER, Telegram and
Elasticsearch. Processor applications own source selection and stage work;
they use these primitives to create immutable batches and advance them safely.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextlib import closing
from datetime import timedelta

from celery import current_app
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from app_base.models import GeneralEncoder
from app_scheduler.contracts import (
    ProcessingSummary,
    StageSummary,
    empty_processing_summary,
)
from app_scheduler.db import lock_batch_and_job, retry_on_deadlock
from app_scheduler.models import ProcessingBatch, ProcessingHandoff, ProcessingJob
from app_scheduler.schedule_policy import (
    normalize_lifecycle_options,
    retry_deadline_for,
)


class InvalidWorkflow(ValueError):
    """A processor attempted to create or run an invalid workflow."""


class StaleStageError(RuntimeError):
    """A late task no longer owns the batch stage it tried to mutate."""


class InvalidJobAction(ValueError):
    """An operator action is not valid for the Job's current lifecycle."""


def stable_fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            cls=GeneralEncoder,
        ).encode("utf-8")
    ).hexdigest()


def build_job_fingerprint(*, processor: str, mode: str, workflow: list[str], filters: dict) -> str:
    return stable_fingerprint(
        {
            "processor": processor,
            "mode": mode,
            "workflow": workflow,
            "filters": filters,
        }
    )


def estimate_batches(total_items: int, batch_size: int) -> int:
    """Return the number of batches needed for a known item count."""

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if total_items < 0:
        raise ValueError("total_items cannot be negative")
    return math.ceil(total_items / batch_size) if total_items else 0


def set_estimated_batches_total(job: ProcessingJob, total_items: int | None) -> ProcessingJob:
    """Persist a source-count estimate without changing materialized counters."""

    estimate = None if total_items is None else estimate_batches(total_items, job.batch_size)
    job.estimated_batches_total = estimate
    job.save(update_fields=["estimated_batches_total"])
    return job


def _validate_workflow(workflow) -> list[str]:
    if not isinstance(workflow, (list, tuple)) or not workflow:
        raise InvalidWorkflow("workflow must be a non-empty list of stage keys")
    normalized = [str(stage).strip() for stage in workflow]
    if any(not stage for stage in normalized) or len(set(normalized)) != len(normalized):
        raise InvalidWorkflow("workflow stage keys must be non-empty and unique")
    return normalized


def _is_scheduled_policy(policy) -> bool:
    return isinstance(policy, dict) and bool(policy.get("scheduled"))


def _same_scheduled_trigger(existing: ProcessingJob, incoming_policy: dict) -> bool:
    existing_policy = existing.lifecycle_policy or {}
    return (
        _is_scheduled_policy(existing_policy)
        and _is_scheduled_policy(incoming_policy)
        and existing_policy.get("trigger_id") == incoming_policy.get("trigger_id")
    )


def _scheduled_job_is_due_for_finalization(job: ProcessingJob, now) -> bool:
    policy = job.lifecycle_policy or {}
    return bool(
        job.status == ProcessingJob.WAITING_RETRY
        and _is_scheduled_policy(policy)
        and policy.get("auto_finalize_failed", True)
        and job.retry_deadline_at is not None
        and job.retry_deadline_at <= now
    )


@retry_on_deadlock
def create_processing_job(
    *,
    processor: str,
    mode: str,
    workflow: list[str],
    filters: dict | None = None,
    batch_size: int,
    max_inflight_batches: int,
    request_token=None,
    source_snapshot_at=None,
    estimated_batches_total: int | None = None,
    parent_job: ProcessingJob | None = None,
    fingerprint: str | None = None,
    deduplicate: bool = True,
    lifecycle_policy: dict | None = None,
) -> tuple[ProcessingJob, bool]:
    """Create an application-owned Job, deduplicating active equivalents.

    No source query is performed here. The processor invokes its own dispatcher
    after the transaction commits.
    """

    workflow = _validate_workflow(workflow)
    filters = filters or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    if mode not in {ProcessingJob.LIVE, ProcessingJob.BACKFILL}:
        raise ValueError("mode must be live or backfill")
    if batch_size < 1 or max_inflight_batches < 1:
        raise ValueError("batch_size and max_inflight_batches must be positive")
    if estimated_batches_total is not None and estimated_batches_total < 0:
        raise ValueError("estimated_batches_total cannot be negative")

    if lifecycle_policy:
        raw_lifecycle_policy = dict(lifecycle_policy)
        lifecycle_policy = normalize_lifecycle_options(raw_lifecycle_policy)
        for key in ("scheduled", "trigger_id", "schedule"):
            if key in raw_lifecycle_policy:
                lifecycle_policy[key] = raw_lifecycle_policy[key]
        lifecycle_policy["scheduled"] = bool(lifecycle_policy.get("scheduled", False))
    else:
        lifecycle_policy = {}

    fingerprint = fingerprint or build_job_fingerprint(
        processor=processor,
        mode=mode,
        workflow=workflow,
        filters=filters,
    )
    now = timezone.now()
    defaults = {
        "processor": processor,
        "mode": mode,
        "workflow": workflow,
        "status": ProcessingJob.COLLECTING if mode == ProcessingJob.LIVE else ProcessingJob.RUNNING,
        "filters": filters,
        "source_snapshot_at": source_snapshot_at,
        "estimated_batches_total": estimated_batches_total,
        "parent_job": parent_job,
        "fingerprint": fingerprint,
        "batch_size": batch_size,
        "max_inflight_batches": max_inflight_batches,
        "request_token": request_token,
        "started_at": None if mode == ProcessingJob.LIVE else now,
        "lifecycle_policy": lifecycle_policy,
    }

    # A scheduled Beat fire is the handoff point between recurring runs.  Do
    # this small preflight before the uniqueness lookup so a due WAITING_RETRY
    # Job becomes PARTIAL and the current fire can create the next Job.  The
    # actual finalisation remains transactional and idempotent in its helper.
    if deduplicate:
        for _ in range(2):
            existing = (
                ProcessingJob.objects.filter(
                    processor=processor,
                    fingerprint=fingerprint,
                    status__in=ProcessingJob.ACTIVE_STATUSES,
                )
                .first()
            )
            if existing is None:
                break
            if _scheduled_job_is_due_for_finalization(existing, now):
                auto_finalize_scheduled_job(existing.id, now=now)
                continue
            if (
                lifecycle_policy.get("replace_running_job")
                and _same_scheduled_trigger(existing, lifecycle_policy)
                and existing.status in {ProcessingJob.COLLECTING, ProcessingJob.RUNNING}
            ):
                cancel_job(existing.id)
                continue
            return existing, False

    with transaction.atomic():
        if deduplicate:
            existing = (
                ProcessingJob.objects.select_for_update(no_key=True)
                .filter(
                    processor=processor,
                    fingerprint=fingerprint,
                    status__in=ProcessingJob.ACTIVE_STATUSES,
                )
                .first()
            )
            if existing:
                return existing, False
        try:
            with transaction.atomic():
                return ProcessingJob.objects.create(**defaults), True
        except IntegrityError:
            if not deduplicate:
                raise
            return (
                ProcessingJob.objects.get(
                    processor=processor,
                    fingerprint=fingerprint,
                    status__in=ProcessingJob.ACTIVE_STATUSES,
                ),
                False,
            )


def _lease_until(now=None, *, job: ProcessingJob | None = None, stage: str | None = None):
    """Return the execution lease for an opaque stage.

    Scheduler stays domain-agnostic: owning applications configure their stage
    key in ``PROCESSING_STAGE_TASKS``.  A scan can therefore have a much longer
    lease than a NER store operation without teaching the scheduler about
    Telegram or NER.
    """

    seconds = settings.PROCESSING_BATCH_LEASE_SECONDS
    if stage:
        config = getattr(settings, "PROCESSING_STAGE_TASKS", {}).get(stage, {})
        seconds = int(config.get("lease_seconds", seconds))
    return (now or timezone.now()) + timedelta(seconds=seconds)


def _stage_config(job: ProcessingJob, stage: str) -> dict:
    config = getattr(settings, "PROCESSING_STAGE_TASKS", {}).get(stage)
    if not isinstance(config, dict) or not config.get("task"):
        raise InvalidWorkflow(f"No task configuration exists for stage {stage!r}")
    queue = config.get("queues", {}).get(job.mode, config.get("queue"))
    if not queue:
        raise InvalidWorkflow(f"No queue configuration exists for stage {stage!r}")
    return {"task": config["task"], "queue": queue}


def enqueue_batch_stage(batch_id) -> None:
    """Publish only a batch UUID to the task configured for its current stage."""

    batch = ProcessingBatch.objects.select_related("job").get(pk=batch_id)
    if batch.status != ProcessingBatch.PENDING or not batch.current_stage:
        return
    config = _stage_config(batch.job, batch.current_stage)
    current_app.send_task(config["task"], args=[str(batch.id)], queue=config["queue"])


def enqueue_source_dispatch(job_id) -> None:
    """Nudge a processor-owned source dispatcher, when that processor has one."""

    job = ProcessingJob.objects.only("id", "processor", "mode", "status").get(pk=job_id)
    if job.mode != ProcessingJob.BACKFILL or job.status not in {
        ProcessingJob.RUNNING,
        ProcessingJob.COLLECTING,
    }:
        return
    config = getattr(settings, "PROCESSING_SOURCE_DISPATCH_TASKS", {}).get(job.processor)
    if not config:
        return
    current_app.send_task(config["task"], args=[str(job.id)], queue=config.get("queue", "default"))


def enqueue_handoff_dispatch(job_id) -> None:
    """Nudge generic handoff materialisation for a child Job."""

    current_app.send_task(
        "app_scheduler.tasks.dispatch_processing_handoffs",
        args=[str(job_id)],
        queue="default",
    )


def _schedule_outgoing_handoff_dispatches(source_batch_id) -> None:
    """Wake child Jobs when this source Batch becomes terminal."""

    target_job_ids = ProcessingHandoff.objects.filter(
        source_batch_id=source_batch_id,
        status=ProcessingHandoff.PENDING,
    ).values_list("target_job_id", flat=True).distinct()
    for target_job_id in target_job_ids:
        transaction.on_commit(
            lambda job_id=target_job_id: enqueue_handoff_dispatch(job_id)
        )


def _queue_batch_locked(batch: ProcessingBatch, now=None) -> None:
    """Make the current stage durable and publish it only after commit."""

    now = now or timezone.now()
    if batch.status in ProcessingBatch.TERMINAL_STATUSES or not batch.current_stage:
        return
    # A stage can finish just after an operator pauses its Job. Do not leave a
    # broker delivery/lease behind in that case: ``resume_job`` will publish
    # this ordinary pending Batch again.
    if not ProcessingJob.objects.filter(
        pk=batch.job_id,
        status__in={ProcessingJob.COLLECTING, ProcessingJob.RUNNING},
    ).exists():
        batch.status = ProcessingBatch.PENDING
        batch.queued_at = None
        batch.run_after = None
        batch.lease_until = None
        batch.lease_token = ""
        batch.save(
            update_fields=["status", "queued_at", "run_after", "lease_until", "lease_token", "updated_at"]
        )
        return
    batch.status = ProcessingBatch.PENDING
    batch.queued_at = now
    batch.run_after = None
    batch.lease_until = _lease_until(now, job=batch.job, stage=batch.current_stage)
    batch.lease_token = ""
    batch.save(
        update_fields=[
            "status", "queued_at", "run_after", "lease_until", "lease_token", "updated_at",
        ]
    )
    transaction.on_commit(lambda batch_id=batch.id: enqueue_batch_stage(batch_id))


def _next_ordinal(job: ProcessingJob) -> int:
    return (job.batches.aggregate(max_ordinal=Max("ordinal"))["max_ordinal"] or 0) + 1


def create_batch_locked(
    job: ProcessingJob,
    *,
    item_ids: list[str],
    processor_metadata: dict | None = None,
    processor_version: str = "",
    item_content_type=None,
    resource_key: str = "",
    source_handoff: ProcessingHandoff | None = None,
    recover_on_lease_expiry: bool = True,
    schedule_dispatch: bool = True,
    now=None,
) -> ProcessingBatch:
    """Create an unqueued batch while its Job row is locked by the caller.

    A live scan may register much more input than its current capacity. The
    scheduler queues only a bounded number of those pending batches later.
    """

    if not item_ids:
        raise ValueError("A ProcessingBatch must contain at least one item")
    workflow = _validate_workflow(job.workflow)
    batch = ProcessingBatch.objects.create(
        job=job,
        ordinal=_next_ordinal(job),
        item_ids=[str(item_id) for item_id in item_ids],
        item_count=len(item_ids),
        item_content_type=item_content_type,
        resource_key=resource_key or "",
        source_handoff=source_handoff,
        recover_on_lease_expiry=recover_on_lease_expiry,
        current_stage=workflow[0],
        processor_metadata=processor_metadata or {},
        processor_version=processor_version,
    )
    if schedule_dispatch:
        transaction.on_commit(lambda job_id=job.id: dispatch_ready_batches(job_id))
    return batch


def active_batch_count(job: ProcessingJob) -> int:
    return job.batches.filter(
        Q(status=ProcessingBatch.PENDING, queued_at__isnull=False)
        | Q(status__in=(ProcessingBatch.RUNNING, ProcessingBatch.RETRY_WAIT))
    ).count()


def _active_resource_keys(job: ProcessingJob) -> set[str]:
    """Return resource keys already owned by dispatched/running batches.

    Resource serialization is intentionally Job-scoped.  A Telegram session can
    therefore serve two independently requested Jobs when the caller explicitly
    permits that, while batches belonging to one Job remain sequential.
    """

    return set(
        job.batches.filter(
            resource_key__gt="",
        ).filter(
            Q(status=ProcessingBatch.PENDING, queued_at__isnull=False)
            | Q(status__in=(ProcessingBatch.RUNNING, ProcessingBatch.RETRY_WAIT))
        ).values_list("resource_key", flat=True)
    )


def available_new_batch_slots(job: ProcessingJob) -> int:
    return max(job.max_inflight_batches - active_batch_count(job), 0)


def _seal_child_inputs_locked(job: ProcessingJob, now) -> None:
    """Close direct child input only after this Job can emit no more handoffs."""

    child_input_outcome = (
        ProcessingJob.INPUT_PARTIAL
        if job.input_outcome == ProcessingJob.INPUT_PARTIAL
        or job.failures_abandoned_at is not None
        else ProcessingJob.INPUT_COMPLETE
    )
    for child in job.child_jobs.select_for_update(no_key=True).order_by("created_at", "id"):
        changed = []
        if child.input_closed_at is None:
            child.input_closed_at = now
            changed.append("input_closed_at")
        if (
            child_input_outcome == ProcessingJob.INPUT_PARTIAL
            and child.input_outcome != ProcessingJob.INPUT_PARTIAL
        ):
            child.input_outcome = ProcessingJob.INPUT_PARTIAL
            changed.append("input_outcome")
        if child.status == ProcessingJob.COLLECTING:
            child.status = ProcessingJob.RUNNING
            child.started_at = now
            changed.extend(["status", "started_at"])
        if not changed:
            continue
        child.save(update_fields=[*changed, "updated_at"])
        transaction.on_commit(lambda job_id=child.id: dispatch_handoffs(job_id))
        # An empty child has no handoff task to drive its lifecycle.  Refresh it
        # explicitly so an empty Scan can finish rather than stay ``running``.
        transaction.on_commit(lambda job_id=child.id: refresh_processing_job(job_id))


def _reopen_descendant_inputs_locked(jobs: list[ProcessingJob], now) -> None:
    """Reopen descendant input when a terminal parent batch is retried.

    A failed parent batch may have caused the parent to look source-finished,
    which seals and can complete its child Jobs.  Retrying that parent batch
    makes the parent capable of producing new handoffs again, so the affected
    descendants must accept them until the retried source is terminal.

    Existing descendant batches are deliberately preserved: only the input
    boundary and lifecycle state are reopened.
    """

    for child in jobs[1:]:
        if child.status == ProcessingJob.CANCELLED or child.failures_abandoned_at is not None:
            continue
        if (
            child.input_closed_at is None
            and child.source_finished_at is None
            and child.completed_at is None
        ):
            continue

        child.input_closed_at = None
        child.source_finished_at = None
        child.completed_at = None
        if child.status in {ProcessingJob.COMPLETED, ProcessingJob.FAILED}:
            child.status = ProcessingJob.RUNNING if child.batches.exists() else ProcessingJob.COLLECTING
            child.started_at = child.started_at or now
        child.save(
            update_fields=[
                "input_closed_at", "source_finished_at", "completed_at", "status",
                "started_at", "updated_at",
            ]
        )
        transaction.on_commit(lambda job_id=child.id: dispatch_handoffs(job_id))
        transaction.on_commit(lambda job_id=child.id: dispatch_ready_batches(job_id))


def _refresh_descendant_counters_locked(job: ProcessingJob) -> None:
    """Refresh a Job's cached counters for all of its descendants.

    This intentionally reads child rows while holding only the parent Job lock.
    A child batch task already holds its own Job lock; trying to lock its parent
    in that same transaction can deadlock with a root task sealing that child
    (root -> child versus child -> root).  The child schedules a parent refresh
    *after* its transaction commits, so the read below sees a stable result and
    no cross-tree lock order is required.
    """

    totals = job.child_jobs.aggregate(
        processed=Sum("processed_items"),
        descendant_processed=Sum("descendant_processed_items"),
        failed=Sum("failed_items"),
        descendant_failed=Sum("descendant_failed_items"),
        deferred=Sum("deferred_items"),
        descendant_deferred=Sum("descendant_deferred_items"),
        skipped=Sum("skipped_items"),
        descendant_skipped=Sum("descendant_skipped_items"),
    )
    job.descendant_processed_items = (totals["processed"] or 0) + (totals["descendant_processed"] or 0)
    job.descendant_failed_items = (totals["failed"] or 0) + (totals["descendant_failed"] or 0)
    job.descendant_deferred_items = (totals["deferred"] or 0) + (totals["descendant_deferred"] or 0)
    job.descendant_skipped_items = (totals["skipped"] or 0) + (totals["descendant_skipped"] or 0)


def _schedule_parent_refresh(job: ProcessingJob) -> None:
    """Refresh ancestor dashboards after this Job's transaction commits."""

    if job.parent_job_id:
        transaction.on_commit(
            lambda parent_id=job.parent_job_id: refresh_processing_job(parent_id)
        )


def _refresh_job_locked(job: ProcessingJob, now=None) -> ProcessingJob:
    now = now or timezone.now()
    summaries = job.batches.values("status").annotate(batches=Count("id"), items=Sum("item_count"))
    by_status = {row["status"]: row for row in summaries}
    job.total_batches = sum(row["batches"] for row in summaries)
    job.total_items = sum(row["items"] or 0 for row in summaries)
    job.completed_batches = by_status.get(ProcessingBatch.COMPLETED, {}).get("batches", 0)
    job.failed_batches = by_status.get(ProcessingBatch.FAILED, {}).get("batches", 0)
    totals = job.batches.aggregate(
        processed=Sum("processed_count"),
        failed=Sum("failed_count"),
        deferred=Sum("deferred_count"),
        skipped=Sum("skipped_count"),
    )
    job.processed_items = totals["processed"] or 0
    job.failed_items = totals["failed"] or 0
    job.deferred_items = totals["deferred"] or 0
    job.skipped_items = totals["skipped"] or 0
    _refresh_descendant_counters_locked(job)

    # A child Job can have durable input that has not been materialised as a
    # Batch yet.  Treat those handoffs as in-flight work as well; otherwise a
    # just-sealed Scan could complete an empty child before its committed scan
    # output gets converted into the first message Batch.
    non_terminal = (
        job.batches.exclude(status__in=ProcessingBatch.TERMINAL_STATUSES).exists()
        or job.incoming_handoffs.filter(status=ProcessingHandoff.PENDING).exists()
    )
    retryable_failures = bool(
        job.failed_batches and job.failures_abandoned_at is None
    )
    # FAILED is a terminal Batch state for accounting, but not for the Job
    # lifecycle while the operator can still retry it.  Only an explicit
    # discard decision turns those failures into final output.
    source_finished = bool(job.input_closed_at and not non_terminal and not retryable_failures)
    if source_finished and job.source_finished_at is None:
        job.source_finished_at = now
        _seal_child_inputs_locked(job, now)
    waiting_for_retry = bool(retryable_failures and not non_terminal)

    # Lifecycle refreshes are also scheduled after child changes.  They must
    # never rewrite a terminal operator outcome (notably ``cancelled``) as
    # ``completed`` merely because all of its batches are terminal too.
    if job.status in {
        ProcessingJob.CANCELLED,
        ProcessingJob.COMPLETED,
        ProcessingJob.FAILED,
        ProcessingJob.PARTIAL,
    }:
        pass
    # Pausing is an operator decision, not a transient execution state.  A
    # delivery that was already running when the pause was requested may still
    # finish, but that must not silently reactivate or complete the Job.  A
    # later ``resume_job`` refreshes the lifecycle and settles it normally.
    elif job.status == ProcessingJob.PAUSED:
        pass
    elif job.status == ProcessingJob.CANCELLING and not non_terminal:
        job.status = ProcessingJob.CANCELLED
        job.completed_at = now
    elif waiting_for_retry:
        job.status = ProcessingJob.WAITING_RETRY
        job.completed_at = None
    elif source_finished:
        # A Job finishes its own workflow independently of its descendants.
        # Closing a scan releases its schedule fingerprint; already persisted
        # messages retain their separate NER/index Job and retry lifecycle.
        job.status = (
            ProcessingJob.PARTIAL
            if job.failures_abandoned_at is not None
            else ProcessingJob.COMPLETED
        )
        job.completed_at = job.completed_at or now
    if job.status == ProcessingJob.WAITING_RETRY:
        # Set the deadline only on entry.  A Job that remains waiting must
        # retain the same handoff window; otherwise each reconciliation pass
        # would move the deadline forward forever.
        if job.retry_deadline_at is None:
            job.retry_deadline_at = retry_deadline_for(job.lifecycle_policy, now)
    else:
        job.retry_deadline_at = None

    job.save(
        update_fields=[
            "total_items", "processed_items", "failed_items", "deferred_items", "skipped_items",
            "total_batches", "completed_batches", "failed_batches", "source_finished_at",
            "input_outcome", "failures_abandoned_at",
            "retry_deadline_at",
            "descendant_processed_items", "descendant_failed_items",
            "descendant_deferred_items", "descendant_skipped_items",
            "status", "completed_at", "updated_at",
        ]
    )
    _schedule_parent_refresh(job)
    return job


def refresh_job_locked(job: ProcessingJob, now=None) -> ProcessingJob:
    """Public locked variant for processor code that created batches."""

    return _refresh_job_locked(job, now)


@retry_on_deadlock
def refresh_processing_job(job_id) -> ProcessingJob:
    """Refresh lifecycle/counters for a Job after a child changed state."""

    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job_id)
        return _refresh_job_locked(job)


def begin_batch_stage_locked(batch: ProcessingBatch, stage: str, now=None) -> str | None:
    """Acquire a short DB transition plus a durable execution lease.

    The caller must have locked ``batch`` with ``select_for_update``. Returning
    ``None`` means another delivery owns the stage or it is not runnable.
    """

    now = now or timezone.now()
    if batch.status in ProcessingBatch.TERMINAL_STATUSES:
        return None
    if batch.job.status in {
        ProcessingJob.PAUSED,
        ProcessingJob.CANCELLING,
        ProcessingJob.WAITING_RETRY,
    }:
        return None
    if batch.current_stage != stage:
        return None
    if batch.status == ProcessingBatch.RUNNING and batch.lease_until and batch.lease_until > now:
        return None
    if batch.status == ProcessingBatch.RETRY_WAIT and batch.run_after and batch.run_after > now:
        return None
    if batch.status not in {ProcessingBatch.PENDING, ProcessingBatch.RETRY_WAIT, ProcessingBatch.RUNNING}:
        return None

    attempts = dict(batch.stage_attempts or {})
    attempts[stage] = int(attempts.get(stage, 0)) + 1
    token = uuid.uuid4().hex
    batch.status = ProcessingBatch.RUNNING
    batch.queued_at = None
    batch.run_after = None
    batch.lease_token = token
    batch.lease_until = _lease_until(now, job=batch.job, stage=stage)
    batch.stage_attempts = attempts
    batch.save(
        update_fields=[
            "status", "queued_at", "run_after", "lease_token", "lease_until",
            "stage_attempts", "updated_at",
        ]
    )
    return token


@retry_on_deadlock
def begin_batch_stage(batch_id, stage: str):
    """Acquire a stage in its own retryable transaction, returning its lease."""

    batch = lock_batch_and_job(batch_id)
    token = begin_batch_stage_locked(batch, stage)
    return batch, token


def ensure_stage_lease_locked(batch: ProcessingBatch, *, stage: str, token: str) -> None:
    if batch.current_stage != stage or batch.status != ProcessingBatch.RUNNING or batch.lease_token != token:
        raise StaleStageError(f"Batch {batch.id} no longer owns stage {stage}")


def complete_batch_stage_locked(
    batch: ProcessingBatch,
    *,
    stage: str,
    token: str,
    processed_count: int | None = None,
    failed_count: int | None = None,
    deferred_count: int | None = None,
    skipped_count: int | None = None,
    stage_summary: StageSummary | None = None,
    now=None,
) -> ProcessingBatch:
    """Advance a locked batch to its next opaque workflow stage or completion."""

    ensure_stage_lease_locked(batch, stage=stage, token=token)
    now = now or timezone.now()
    workflow = _validate_workflow(batch.job.workflow)
    stage_index = workflow.index(stage)
    completed = list(batch.completed_stages or [])
    if stage not in completed:
        completed.append(stage)
    batch.completed_stages = completed
    summary = ProcessingSummary.model_validate(
        empty_processing_summary() if batch.result_summary is None else batch.result_summary
    )
    if stage_summary is not None:
        summary.stages[stage] = stage_summary
    batch.result_summary = summary.model_dump(mode="json")
    if processed_count is not None:
        batch.processed_count = processed_count
    if failed_count is not None:
        batch.failed_count = failed_count
    if deferred_count is not None:
        batch.deferred_count = deferred_count
    if skipped_count is not None:
        batch.skipped_count = skipped_count
    # A retry may have recorded a transient error for this stage.  Once the
    # stage succeeds, the Batch reflects its current state rather than an old
    # failure; the Job-level last_error remains the compact operational trail.
    batch.last_error = ""

    if stage_index + 1 < len(workflow):
        batch.current_stage = workflow[stage_index + 1]
        batch.lease_token = ""
        batch.lease_until = None
        batch.save(
            update_fields=[
                "current_stage", "completed_stages", "lease_token",
                "lease_until", "processed_count", "failed_count", "deferred_count", "skipped_count",
                "result_summary", "last_error", "updated_at",
            ]
        )
        _queue_batch_locked(batch, now)
    else:
        batch.status = ProcessingBatch.COMPLETED
        batch.current_stage = ""
        if processed_count is None and failed_count is None and deferred_count is None and skipped_count is None:
            batch.processed_count = batch.item_count
            batch.failed_count = 0
            batch.deferred_count = 0
            batch.skipped_count = 0
        batch.lease_token = ""
        batch.lease_until = None
        batch.run_after = None
        batch.completed_at = now
        batch.save(
            update_fields=[
                "status", "current_stage", "completed_stages", "processed_count", "failed_count",
                "deferred_count", "skipped_count", "result_summary",
                "lease_token", "lease_until", "run_after", "completed_at", "last_error",
                "updated_at",
            ]
        )
        _refresh_job_locked(batch.job, now)
        transaction.on_commit(lambda job_id=batch.job_id: dispatch_ready_batches(job_id))
        transaction.on_commit(lambda job_id=batch.job_id: dispatch_handoffs(job_id))
        transaction.on_commit(lambda job_id=batch.job_id: enqueue_source_dispatch(job_id))
        _schedule_outgoing_handoff_dispatches(batch.id)
    return batch


def restart_batch_from_stage_locked(
    batch: ProcessingBatch,
    *,
    stage: str,
    now=None,
) -> ProcessingBatch:
    """Restart a locked Batch at a declared stage after owner input changed."""

    workflow = _validate_workflow(batch.job.workflow)
    if stage not in workflow:
        raise InvalidWorkflow(f"Stage {stage!r} is not part of Job {batch.job_id}")
    now = now or timezone.now()
    batch.current_stage = stage
    batch.completed_stages = workflow[:workflow.index(stage)]
    batch.lease_token = ""
    batch.lease_until = None
    batch.run_after = None
    batch.last_error = "Input changed; restarting from this stage"
    batch.save(
        update_fields=[
            "current_stage", "completed_stages", "lease_token", "lease_until",
            "run_after", "last_error", "updated_at",
        ]
    )
    _queue_batch_locked(batch, now)
    return batch


def retry_batch_stage_locked(
    batch: ProcessingBatch,
    *,
    stage: str,
    token: str | None,
    error: Exception | str,
    countdown: int,
    now=None,
) -> bool:
    """Persist a retry without allowing a superseded delivery to regress state."""

    now = now or timezone.now()
    if batch.status in ProcessingBatch.TERMINAL_STATUSES or batch.current_stage != stage:
        return False
    if token is not None and batch.lease_token != token:
        return False
    batch.status = ProcessingBatch.RETRY_WAIT
    batch.queued_at = None
    batch.run_after = now + timedelta(seconds=countdown)
    batch.lease_token = ""
    batch.lease_until = _lease_until(batch.run_after, job=batch.job, stage=stage)
    batch.last_error = str(error)
    ProcessingJob.objects.filter(pk=batch.job_id).update(last_error=str(error), updated_at=now)
    batch.save(
        update_fields=[
            "status", "queued_at", "run_after", "lease_token", "lease_until", "last_error", "updated_at",
        ]
    )
    return True


def fail_batch_stage_locked(
    batch: ProcessingBatch,
    *,
    stage: str,
    token: str | None,
    error: Exception | str,
    failed_count: int | None = None,
    now=None,
) -> bool:
    """Fail the leased stage, preserving the owner's confirmed partial work.

    ``failed_count`` optionally counts outstanding input instead of the whole
    batch. Return False for a superseded delivery; otherwise persist failure,
    refresh the Job and queue coordination after commit. Invalid counts raise
    ValueError without changing the batch.
    """
    now = now or timezone.now()
    if batch.status in ProcessingBatch.TERMINAL_STATUSES or batch.current_stage != stage:
        return False
    if token is not None and batch.lease_token != token:
        return False
    if failed_count is not None and not 0 <= failed_count <= batch.item_count:
        raise ValueError("failed_count must be between zero and batch.item_count")
    batch.status = ProcessingBatch.FAILED
    batch.failed_count = batch.item_count if failed_count is None else failed_count
    batch.lease_token = ""
    batch.lease_until = None
    batch.run_after = None
    batch.last_error = str(error)
    batch.completed_at = now
    ProcessingJob.objects.filter(pk=batch.job_id).update(last_error=str(error), updated_at=now)
    batch.save(
        update_fields=[
            "status", "failed_count", "lease_token", "lease_until", "run_after", "last_error",
            "completed_at", "updated_at",
        ]
    )
    _refresh_job_locked(batch.job, now)
    transaction.on_commit(lambda job_id=batch.job_id: dispatch_ready_batches(job_id))
    transaction.on_commit(lambda job_id=batch.job_id: dispatch_handoffs(job_id))
    transaction.on_commit(lambda job_id=batch.job_id: enqueue_source_dispatch(job_id))
    _schedule_outgoing_handoff_dispatches(batch.id)
    return True


@retry_on_deadlock
def dispatch_ready_batches(job_id) -> list[str]:
    """Publish already-created stages. Source discovery stays with the owner."""

    queued = []
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job_id)
        if job.status not in {ProcessingJob.COLLECTING, ProcessingJob.RUNNING}:
            return queued
        now = timezone.now()
        capacity = max(job.max_inflight_batches - active_batch_count(job), 0)
        if capacity == 0:
            return queued
        active_resources = _active_resource_keys(job)
        while len(queued) < capacity:
            candidates = job.batches.filter(status=ProcessingBatch.PENDING, queued_at__isnull=True)
            if active_resources:
                candidates = candidates.filter(Q(resource_key="") | ~Q(resource_key__in=active_resources))
            candidate = (
                candidates.select_for_update(no_key=True, skip_locked=True)
                .defer("item_ids", "processor_metadata", "result_summary")
                .order_by("ordinal").first()
            )
            if candidate is None:
                break
            candidate.job = job
            _queue_batch_locked(candidate, now)
            queued.append(str(candidate.id))
            if candidate.resource_key:
                active_resources.add(candidate.resource_key)
        _refresh_job_locked(job, now)
    return queued


def create_handoffs_locked(
    source_batch: ProcessingBatch,
    *,
    target_job: ProcessingJob,
    item_content_type,
    item_ids: list[str],
    chunk_size: int | None = None,
) -> list[ProcessingHandoff]:
    """Persist output chunks for a child Job while the source transaction lives.

    This deliberately does not create target batches itself: capacity belongs to
    the target Job and is applied later by ``dispatch_handoffs``.  A worker crash
    between commit and publication is harmless because Beat will discover the
    pending handoff.
    """

    ids = list(dict.fromkeys(str(item_id) for item_id in item_ids or []))
    if not ids:
        return []
    # Pausing a composed pipeline must stop publication, not discard output
    # produced by a source task that was already in flight.  Keep the handoff
    # durable while the child is paused; ``dispatch_handoffs`` will leave it
    # pending until ``resume_job`` wakes that child again.  Cancelling is
    # different: it deliberately cuts the downstream edge.
    if target_job.status not in {
        ProcessingJob.COLLECTING,
        ProcessingJob.RUNNING,
        ProcessingJob.PAUSED,
        ProcessingJob.WAITING_RETRY,
    }:
        return []
    chunk_size = int(chunk_size or target_job.batch_size)
    if chunk_size < 1:
        raise ValueError("handoff chunk_size must be positive")
    handoffs = [
        ProcessingHandoff(
            source_batch=source_batch,
            target_job=target_job,
            item_content_type=item_content_type,
            item_ids=ids[offset:offset + chunk_size],
            item_count=len(ids[offset:offset + chunk_size]),
        )
        for offset in range(0, len(ids), chunk_size)
    ]
    ProcessingHandoff.objects.bulk_create(handoffs, batch_size=settings.BULK_BATCH_SIZE)
    transaction.on_commit(lambda job_id=target_job.id: enqueue_handoff_dispatch(job_id))
    return handoffs


def _materialize_handoff_group_locked(
    job: ProcessingJob,
    *,
    handoffs: list[ProcessingHandoff],
    item_limit: int,
    item_content_type,
    batch_size: int,
    now,
) -> list[ProcessingBatch]:
    """Pack one source Batch's handoff items into child batches.

    Handoff rows are split when a row crosses a child batch boundary. Each
    resulting fragment keeps the same source Batch and can therefore be
    audited against the exact parent that produced it.
    """

    chunks: list[list[tuple[ProcessingHandoff, list[str]]]] = []
    new_fragments = []
    consumed = 0
    current_chunk: list[tuple[ProcessingHandoff, list[str]]] = []
    current_count = 0

    def append_piece(handoff, item_ids, *, is_original):
        if is_original:
            fragment = handoff
        else:
            fragment = ProcessingHandoff(
                source_batch_id=handoff.source_batch_id,
                target_job_id=handoff.target_job_id,
                item_content_type_id=handoff.item_content_type_id,
                item_ids=item_ids,
                item_count=len(item_ids),
                created_at=handoff.created_at,
            )
            new_fragments.append(fragment)
        fragment.item_ids = item_ids
        fragment.item_count = len(item_ids)
        return fragment

    for handoff in handoffs:
        original_ids = [str(item_id) for item_id in handoff.item_ids or []]
        offset = 0
        first_piece = True
        while offset < len(original_ids) and consumed < item_limit:
            chunk_room = batch_size - current_count
            take_count = min(chunk_room, item_limit - consumed, len(original_ids) - offset)
            item_ids = original_ids[offset:offset + take_count]
            fragment = append_piece(handoff, item_ids, is_original=first_piece)
            first_piece = False
            current_chunk.append((fragment, item_ids))
            current_count += take_count
            consumed += take_count
            offset += take_count
            if current_count == batch_size:
                chunks.append(current_chunk)
                current_chunk = []
                current_count = 0

        if offset < len(original_ids):
            remainder = original_ids[offset:]
            if first_piece:
                # This row was selected at the edge of the requested limit,
                # but none of its items belong to this dispatch.
                continue
            append_piece(handoff, remainder, is_original=False)

    if current_chunk:
        chunks.append(current_chunk)

    if new_fragments:
        ProcessingHandoff.objects.bulk_create(new_fragments, batch_size=settings.BULK_BATCH_SIZE)

    batches = []
    for chunk in chunks:
        item_ids = [item_id for _, part_ids in chunk for item_id in part_ids]
        contributors = [fragment for fragment, _ in chunk]
        batch = create_batch_locked(
            job,
            item_ids=item_ids,
            item_content_type=item_content_type,
            source_handoff=contributors[0],
            schedule_dispatch=False,
        )
        for fragment, _ in chunk:
            fragment.status = ProcessingHandoff.MATERIALIZED
            fragment.target_batch = batch
            fragment.materialized_at = now
            # The target Batch now owns the canonical item list.
            fragment.item_ids = []
            fragment.save(
                update_fields=[
                    "status", "target_batch", "materialized_at", "item_ids", "item_count", "updated_at",
                ]
            )
        batches.append(batch)
    return batches


@retry_on_deadlock
def dispatch_handoffs(job_id) -> list[str]:
    """Pack same-source handoffs, flushing partial input when that source ends."""

    materialized = []
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job_id)
        if job.status not in {ProcessingJob.COLLECTING, ProcessingJob.RUNNING}:
            return materialized
        capacity = available_new_batch_slots(job)
        if capacity > 0:
            batch_size = max(int(job.batch_size), 1)
            groups = list(
                ProcessingHandoff.objects.filter(
                    target_job=job,
                    status=ProcessingHandoff.PENDING,
                )
                .values("source_batch_id", "source_batch__status", "item_content_type_id")
                .annotate(item_total=Sum("item_count"), first_created=Min("created_at"))
                .order_by("first_created", "source_batch_id", "item_content_type_id")
            )
            now = timezone.now()
            remaining_capacity = capacity
            for group in groups:
                total_items = int(group["item_total"] or 0)
                if total_items <= 0:
                    continue
                source_finished = group["source_batch__status"] in ProcessingBatch.TERMINAL_STATUSES
                available_group_batches = total_items // batch_size
                if source_finished and total_items % batch_size:
                    available_group_batches += 1
                if not available_group_batches:
                    continue

                group_batch_count = min(available_group_batches, remaining_capacity)
                item_limit = min(total_items, group_batch_count * batch_size)
                group_handoffs = []
                selected_items = 0
                pending_rows = (
                    ProcessingHandoff.objects.select_for_update(no_key=True, of=("self",))
                    .select_related("item_content_type")
                    .filter(
                        target_job=job,
                        source_batch_id=group["source_batch_id"],
                        item_content_type_id=group["item_content_type_id"],
                        status=ProcessingHandoff.PENDING,
                    )
                    .order_by("created_at", "id")
                )
                # A cursor fetch locks its whole fetched page. Fetch only the
                # rows needed by this dispatch, and close it before writes.
                with closing(pending_rows.iterator(chunk_size=1)) as rows:
                    for handoff in rows:
                        group_handoffs.append(handoff)
                        selected_items += len(handoff.item_ids or [])
                        if selected_items >= item_limit:
                            break
                batches = _materialize_handoff_group_locked(
                    job,
                    handoffs=group_handoffs,
                    item_limit=item_limit,
                    item_content_type=group_handoffs[0].item_content_type,
                    batch_size=batch_size,
                    now=now,
                )
                materialized.extend(str(batch.id) for batch in batches)
                remaining_capacity -= len(batches)
                if not remaining_capacity:
                    break
        if materialized:
            transaction.on_commit(lambda target_job_id=job.id: dispatch_ready_batches(target_job_id))
        _refresh_job_locked(job)
    return materialized


@retry_on_deadlock
def seal_job(job_id) -> ProcessingJob:
    """Close live input; the workflow continues until all batches terminate."""

    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job_id)
        if job.input_closed_at is None:
            now = timezone.now()
            job.input_closed_at = now
            if job.status == ProcessingJob.COLLECTING:
                job.status = ProcessingJob.RUNNING
                job.started_at = now
            job.save(update_fields=["input_closed_at", "status", "started_at", "updated_at"])
        _refresh_job_locked(job)
        transaction.on_commit(lambda job_id=job.id: dispatch_ready_batches(job_id))
        return job


def _job_tree_ids(job_id) -> list:
    """Return a root-first list of all descendants without recursive SQL."""

    ids = [job_id]
    frontier = [job_id]
    while frontier:
        children = list(ProcessingJob.objects.filter(parent_job_id__in=frontier).values_list("id", flat=True))
        ids.extend(children)
        frontier = children
    return ids


@retry_on_deadlock
def pause_job(job_id) -> ProcessingJob:
    with transaction.atomic():
        ids = _job_tree_ids(job_id)
        jobs = list(ProcessingJob.objects.select_for_update(no_key=True).filter(pk__in=ids).order_by("created_at", "id"))
        for job in jobs:
            if job.status in {ProcessingJob.COLLECTING, ProcessingJob.RUNNING}:
                job.status = ProcessingJob.PAUSED
                job.save(update_fields=["status", "updated_at"])
                # Deliveries already sitting in Redis can still arrive.  They
                # will harmlessly no-op in ``begin_batch_stage_locked`` while
                # paused; clearing the durable queue marker lets resume publish
                # a fresh delivery immediately rather than waiting for expiry.
                job.batches.filter(status=ProcessingBatch.PENDING).update(
                    queued_at=None,
                    lease_token="",
                    lease_until=None,
                    run_after=None,
                )
        job = next(job for job in jobs if job.id == job_id)
        return job


def _resume_paused_jobs_locked(jobs):
    """Resume paused Jobs in an already locked tree and queue their work."""

    for job in jobs:
        if job.status != ProcessingJob.PAUSED:
            continue
        job.status = (
            ProcessingJob.RUNNING
            if job.input_closed_at or job.mode == ProcessingJob.BACKFILL
            else ProcessingJob.COLLECTING
        )
        job.last_error = ""
        job.save(update_fields=["status", "last_error", "updated_at"])
        transaction.on_commit(lambda job_id=job.id: dispatch_ready_batches(job_id))
        transaction.on_commit(lambda job_id=job.id: dispatch_handoffs(job_id))
        transaction.on_commit(lambda job_id=job.id: enqueue_source_dispatch(job_id))


@retry_on_deadlock
def resume_job(job_id) -> ProcessingJob:
    """Resume a paused Job tree and return the refreshed root.

    ``job_id`` identifies the tree. Persist runnable states and queue batch,
    handoff and source coordination after commit. Cursor-driven BACKFILL roots
    resume RUNNING even while input is open. Missing Jobs/database errors
    propagate; already runnable or terminal nodes retain their state.
    """
    with transaction.atomic():
        ids = _job_tree_ids(job_id)
        jobs = list(ProcessingJob.objects.select_for_update(no_key=True).filter(pk__in=ids).order_by("created_at", "id"))
        now = timezone.now()
        _resume_paused_jobs_locked(jobs)
        # A task may have completed while its Job was paused.  Refresh leaves
        # before parents so a resumed root immediately reflects a terminal
        # child instead of waiting for a task that will never be published.
        for job in reversed(jobs):
            _refresh_job_locked(job, now)
        job = next(job for job in jobs if job.id == job_id)
        return job


@retry_on_deadlock
def cancel_job(job_id) -> ProcessingJob:
    with transaction.atomic():
        ids = _job_tree_ids(job_id)
        # Keep the same root-to-leaf lock order used by pause/resume and by a
        # parent sealing its child inputs.  There is no requirement to cancel
        # children first: they receive their cancelled state in this single
        # transaction before any worker can observe the commit.
        jobs = list(ProcessingJob.objects.select_for_update(no_key=True).filter(pk__in=ids).order_by("created_at", "id"))
        now = timezone.now()
        for job in jobs:
            if job.status not in ProcessingJob.ACTIVE_STATUSES:
                continue
            job.status = ProcessingJob.CANCELLING
            job.input_closed_at = job.input_closed_at or now
            job.batches.select_for_update(no_key=True).exclude(status__in=ProcessingBatch.TERMINAL_STATUSES).update(
                status=ProcessingBatch.CANCELLED,
                lease_token="",
                lease_until=None,
                run_after=None,
                completed_at=now,
                last_error="Job cancelled",
            )
            ProcessingHandoff.objects.filter(
                target_job=job,
                status=ProcessingHandoff.PENDING,
            ).update(status=ProcessingHandoff.CANCELLED)
        # Settle leaves first.  This makes the synchronous control endpoint
        # return a cancelled root when all of its children were cancelled in
        # this same transaction, rather than a briefly stale ``cancelling``.
        for job in reversed(jobs):
            if job.status == ProcessingJob.CANCELLING:
                _refresh_job_locked(job, now)
        job = next(job for job in jobs if job.id == job_id)
        return job


def _mark_failed_batches_abandoned_locked(
    job: ProcessingJob,
    now,
    *,
    include_retry_wait: bool = False,
    reason: str = "Failed batches discarded by schedule policy",
) -> bool:
    """Abandon a locked Job's failed work while retaining confirmed outcomes.

    Parameters supply the Job/time and optionally pending retries/reason.
    Return whether failures exist, persist partial input provenance and turn
    retries into failures counting only unconfirmed input. Database errors
    propagate; the caller owns the transaction and Job lock.
    """

    if include_retry_wait:
        retrying = list(
            job.batches.select_for_update(no_key=True).filter(status=ProcessingBatch.RETRY_WAIT)
        )
        for batch in retrying:
            batch.status = ProcessingBatch.FAILED
            batch.failed_count = max(batch.item_count - batch.processed_count - batch.skipped_count - batch.deferred_count, 0)
            batch.lease_token = ""
            batch.lease_until = None
            batch.run_after = None
            batch.completed_at = now
            batch.last_error = reason
            batch.save(
                update_fields=[
                    "status", "failed_count", "lease_token", "lease_until", "run_after",
                    "completed_at", "last_error", "updated_at",
                ]
            )
            _schedule_outgoing_handoff_dispatches(batch.id)

    if not job.batches.filter(status=ProcessingBatch.FAILED).exists():
        return False

    job.failures_abandoned_at = job.failures_abandoned_at or now
    job.input_outcome = ProcessingJob.INPUT_PARTIAL
    job.input_closed_at = job.input_closed_at or now
    job.source_finished_at = None
    job.completed_at = None
    job.retry_deadline_at = None
    if job.status in {
        ProcessingJob.COLLECTING,
        ProcessingJob.RUNNING,
        ProcessingJob.WAITING_RETRY,
        ProcessingJob.COMPLETED,
        ProcessingJob.FAILED,
    }:
        job.status = ProcessingJob.RUNNING
    job.save(
        update_fields=[
            "failures_abandoned_at", "input_outcome", "input_closed_at",
            "source_finished_at", "completed_at", "retry_deadline_at", "status", "updated_at",
        ]
    )
    return True


@retry_on_deadlock
def auto_finalize_scheduled_job(job_id, *, now=None) -> ProcessingJob | None:
    """Finalize only the due scheduled Job's own failed work.

    This is intentionally different from ``cancel_job``: successful batches,
    materialised handoffs and already-running downstream work are preserved.
    Descendants keep their durable batches, retries and handoffs. A future
    scan cannot rediscover messages whose acquisition checkpoint was committed.
    """

    now = now or timezone.now()
    with transaction.atomic():
        tree_ids = _job_tree_ids(job_id)
        jobs = list(
            ProcessingJob.objects.select_for_update(no_key=True)
            .filter(pk__in=tree_ids)
            .order_by("created_at", "id")
        )
        if not jobs:
            return None
        root = next(job for job in jobs if str(job.id) == str(job_id))
        if not _scheduled_job_is_due_for_finalization(root, now):
            return root

        if not _mark_failed_batches_abandoned_locked(root, now, include_retry_wait=True):
            return root

        # Refresh leaves first so parent source boundaries and child partial
        # input markers are propagated in the same transaction.
        for _ in range(max(len(jobs), 1)):
            for child in reversed(jobs[1:]):
                child.refresh_from_db()
                _refresh_job_locked(child, now)
            root.refresh_from_db()
            _refresh_job_locked(root, now)

        transaction.on_commit(lambda root_id=root.id: dispatch_ready_batches(root_id))
        transaction.on_commit(lambda root_id=root.id: dispatch_handoffs(root_id))
        transaction.on_commit(lambda root_id=root.id: enqueue_source_dispatch(root_id))
        return root


@retry_on_deadlock
def discard_failed_job(job_id) -> ProcessingJob:
    """Make failed batches final while preserving them as FAILED audit rows.

    This is an explicit operator decision, distinct from cancellation.  The
    Job closes its input so no later source output is silently introduced,
    marks the input boundary as partial, and lets already materialised child
    work drain.  A child therefore remains COMPLETED when everything it did
    receive succeeded, while carrying ``input_outcome=partial``.
    """

    with transaction.atomic():
        tree_ids = _job_tree_ids(job_id)
        locked_jobs = list(
            ProcessingJob.objects.select_for_update(no_key=True)
            .filter(pk__in=tree_ids)
            .order_by("created_at", "id")
        )
        job = next(job for job in locked_jobs if str(job.id) == str(job_id))
        if job.status == ProcessingJob.PARTIAL and job.failures_abandoned_at is not None:
            return job
        if job.status in {ProcessingJob.CANCELLED, ProcessingJob.CANCELLING}:
            raise InvalidJobAction("A cancelled or cancelling Job cannot discard failed batches.")
        if not job.batches.filter(status=ProcessingBatch.FAILED).exists():
            raise InvalidJobAction("The Job has no failed batches to discard.")

        now = timezone.now()
        _mark_failed_batches_abandoned_locked(job, now)
        _refresh_job_locked(job, now)
        # Settle empty/already-terminal descendants in this same control
        # transaction. Non-empty descendants keep their independent lifecycle;
        # the parent's own partial outcome is already recorded.
        # A refresh can seal the next descendant (parent -> child ->
        # grandchild), so one bottom-up pass is not enough for an empty tree
        # with several stages.  Repeat the bounded tree depth until every
        # newly-sealed descendant has had a chance to settle.
        for _ in range(max(len(locked_jobs), 1)):
            for child in reversed(locked_jobs[1:]):
                # ``_seal_child_inputs_locked`` may have updated this row
                # through the related manager while the in-memory tree
                # snapshot still contains the old input outcome/status.
                child.refresh_from_db()
                _refresh_job_locked(child, now)
            job.refresh_from_db()
            _refresh_job_locked(job, now)
        transaction.on_commit(lambda job_id=job.id: dispatch_ready_batches(job_id))
        transaction.on_commit(lambda job_id=job.id: dispatch_handoffs(job_id))
        transaction.on_commit(lambda job_id=job.id: enqueue_source_dispatch(job_id))
        return job


@retry_on_deadlock
def retry_failed_job(job_id) -> ProcessingJob:
    with transaction.atomic():
        tree_ids = _job_tree_ids(job_id)
        locked_jobs = list(
            ProcessingJob.objects.select_for_update(no_key=True)
            .filter(pk__in=tree_ids)
            .order_by("created_at", "id")
        )
        jobs_by_id = {str(job.id): job for job in locked_jobs}
        jobs = [jobs_by_id[str(tree_id)] for tree_id in tree_ids]
        job = next(job for job in jobs if str(job.id) == str(job_id))
        failed = list(job.batches.select_for_update(no_key=True).filter(status=ProcessingBatch.FAILED))
        now = timezone.now()
        if job.status == ProcessingJob.PARTIAL or job.failures_abandoned_at is not None:
            if failed:
                raise InvalidJobAction("Discarded failed batches cannot be retried.")
            return job
        was_paused = job.status == ProcessingJob.PAUSED
        retrying = bool(
            failed
            and (
                job.status in ProcessingJob.RETRYABLE_STATUSES
                or was_paused
            )
        )
        if retrying:
            if not was_paused:
                job.status = ProcessingJob.RUNNING
            job.completed_at = None
            job.source_finished_at = None
            job.retry_deadline_at = None
            job.last_error = ""
            update_fields = [
                "completed_at", "source_finished_at", "retry_deadline_at", "last_error", "updated_at",
            ]
            if not was_paused:
                update_fields.append("status")
            job.save(update_fields=update_fields)
            _reopen_descendant_inputs_locked(jobs, now)
            # Restarting a Job while leaving paused nodes in its subtree
            # untouched would strand batches and handoffs without consumers.
            # A successful retry therefore resumes every paused node in scope.
            _resume_paused_jobs_locked(jobs if was_paused else jobs[1:])
        for batch in failed:
            batch.status = ProcessingBatch.PENDING
            batch.failed_count = 0
            batch.last_error = ""
            batch.completed_at = None
            batch.run_after = None
            batch.queued_at = None
            batch.lease_token = ""
            batch.lease_until = None
            batch.save(
                update_fields=[
                    "status", "failed_count", "last_error", "completed_at", "run_after", "queued_at",
                    "lease_token", "lease_until", "updated_at",
                ]
            )
        if failed and not was_paused:
            transaction.on_commit(lambda job_id=job.id: dispatch_ready_batches(job_id))
            transaction.on_commit(lambda job_id=job.id: dispatch_handoffs(job_id))
        if retrying:
            for descendant in reversed(jobs[1:]):
                _refresh_job_locked(descendant, now)
        _refresh_job_locked(job, now)
        return job


def retryable_jobs():
    """Jobs whose own failed batches can be retried without reviving a discard."""

    return ProcessingJob.objects.filter(
        failed_batches__gt=0,
        failures_abandoned_at__isnull=True,
        status__in={
            *ProcessingJob.RETRYABLE_STATUSES,
            ProcessingJob.COLLECTING,
            ProcessingJob.RUNNING,
            ProcessingJob.PAUSED,
        },
    )


@retry_on_deadlock
def retry_failed_jobs(job_ids, *, include_paused=True) -> dict:
    """Retry a fixed set of Jobs, keeping successful stages and discard decisions."""

    retried_jobs = 0
    retried_batches = 0
    with transaction.atomic():
        requested_ids = {str(job_id) for job_id in job_ids}
        scope_ids = set()
        for job_id in requested_ids:
            scope_ids.update(_job_tree_ids(job_id))
        # Include intermediate nodes before locking requested descendants.
        # Otherwise a bulk retry could hold a grandchild while waiting for
        # its parent in the opposite order to a concurrent tree control.
        locked = list(
            ProcessingJob.objects.select_for_update(no_key=True)
            .filter(pk__in=scope_ids).order_by("created_at", "id")
        )
        jobs = list(
            job for job in locked
            if str(job.pk) in requested_ids and job.can_retry_failed
            and (include_paused or job.status != ProcessingJob.PAUSED)
        )
        for job in jobs:
            count = job.batches.filter(status=ProcessingBatch.FAILED).count()
            retry_failed_job(job.pk)
            retried_jobs += bool(count)
            retried_batches += count
    return {"jobs": retried_jobs, "batches": retried_batches}


def retry_failed_job_tree(job_id) -> ProcessingJob:
    """Retry recoverable failures in a Job and its descendants from any parent page."""

    retry_failed_jobs(_job_tree_ids(job_id))
    return ProcessingJob.objects.get(pk=job_id)


def processing_capacity() -> dict:
    active_batches = ProcessingBatch.objects.exclude(status__in=ProcessingBatch.TERMINAL_STATUSES)
    active_jobs = ProcessingJob.objects.filter(status__in=ProcessingJob.ACTIVE_STATUSES)
    return {
        "active_jobs": active_jobs.count(),
        "active_batches": active_batches.count(),
        "jobs_by_processor": {
            row["processor"]: row["count"]
            for row in active_jobs.values("processor").annotate(count=Count("id"))
        },
        "batches_by_processor": {
            row["job__processor"]: row["count"]
            for row in active_batches.values("job__processor").annotate(count=Count("id"))
        },
    }


@retry_on_deadlock
def _reconcile_job_batches(job_id, now) -> list[str]:
    """Recover one Job without locking Batches ahead of their owner."""

    job = (
        ProcessingJob.objects.select_for_update(no_key=True, skip_locked=True)
        .filter(pk=job_id).first()
    )
    if job is None or job.status not in {ProcessingJob.COLLECTING, ProcessingJob.RUNNING}:
        return []
    batches = list(
        job.batches.select_for_update(no_key=True, skip_locked=True)
        .filter(Q(lease_until__lt=now) | Q(status=ProcessingBatch.RETRY_WAIT, run_after__lte=now))
        .exclude(status__in=ProcessingBatch.TERMINAL_STATUSES)
        .order_by("ordinal")
    )
    recovered = []
    for batch in batches:
        batch.job = job
        expired = batch.lease_until is not None and batch.lease_until < now
        if expired and not batch.recover_on_lease_expiry:
            batch.status = ProcessingBatch.FAILED
            batch.failed_count = max(batch.item_count - batch.processed_count - batch.skipped_count - batch.deferred_count, 0)
            batch.completed_at = now
            batch.last_error = "Worker lease expired; manual retry required"
            batch.lease_token = ""
            batch.lease_until = None
            batch.run_after = None
            batch.save(update_fields=[
                "status", "failed_count", "lease_token", "lease_until", "run_after",
                "last_error", "completed_at", "updated_at",
            ])
            _schedule_outgoing_handoff_dispatches(batch.id)
        else:
            # Release the old delivery before dispatching through the normal
            # capacity check. A due retry must not bypass max_inflight_batches.
            batch.status = ProcessingBatch.PENDING
            batch.queued_at = None
            batch.run_after = None
            batch.lease_token = ""
            batch.lease_until = None
            batch.save(update_fields=[
                "status", "queued_at", "run_after", "lease_token", "lease_until", "updated_at",
            ])
        recovered.append(str(batch.id))
    if recovered:
        _refresh_job_locked(job, now)
    return recovered


def reconcile_jobs() -> dict:
    """Recover expired leases and due retries using generic durable state.

    Accept no parameters; return counts of recovered batches, nudged Jobs and
    schedule finalizations. Persist repaired leases/failures, preserving partial
    counts, and publish coordination after commit. Database or publication
    errors propagate so the next reconciliation can recover the durable state.
    """

    now = timezone.now()
    recovered = []
    nudged_jobs = set()
    auto_finalized_jobs = 0

    due_scheduled_jobs = list(
        ProcessingJob.objects.filter(
            parent_job__isnull=True,
            status=ProcessingJob.WAITING_RETRY,
            retry_deadline_at__lte=now,
        ).values_list("id", flat=True)
    )
    for job_id in due_scheduled_jobs:
        finalized = auto_finalize_scheduled_job(job_id, now=now)
        if finalized is not None and finalized.status == ProcessingJob.PARTIAL:
            auto_finalized_jobs += 1

    due_job_ids = list(
        ProcessingBatch.objects.filter(
            Q(lease_until__lt=now) | Q(status=ProcessingBatch.RETRY_WAIT, run_after__lte=now),
            job__status__in={ProcessingJob.COLLECTING, ProcessingJob.RUNNING},
        ).exclude(status__in=ProcessingBatch.TERMINAL_STATUSES)
        .order_by().values_list("job_id", flat=True).distinct()
    )
    for job_id in due_job_ids:
        recovered.extend(_reconcile_job_batches(job_id, now))
        nudged_jobs.add(job_id)

    active = ProcessingJob.objects.filter(status__in={ProcessingJob.COLLECTING, ProcessingJob.RUNNING})
    nudged_jobs.update(active.values_list("id", flat=True))
    # Every recovery transaction has committed before coordination begins.
    for job_id in sorted(nudged_jobs, key=str):
        dispatch_ready_batches(job_id)
        dispatch_handoffs(job_id)
        enqueue_source_dispatch(job_id)

    return {
        "recovered_batches": len(recovered),
        "jobs_nudged": len(nudged_jobs),
        "auto_finalized_jobs": auto_finalized_jobs,
    }
