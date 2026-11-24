"""Convert domain records into terminology suitable for operators."""

from django.utils.translation import gettext as _
from pydantic import ValidationError

from app_dashboard.constants import (
    HIDDEN_PARAMETER_FIELDS,
    PIPELINE_FIELD_LABELS,
    batch_status_meta,
    job_status_meta,
    pipeline_label,
    stage_label,
)
from app_dashboard.enums import StageDisplayState
from app_scheduler.contracts import ProcessingSummary, StageSummary
from app_scheduler.enums import BatchStatus
from app_scheduler.models import ProcessingJob
from app_telegram.job_keys import TELEGRAM_INDEX_STAGE


INDEX_DESTINATIONS = {
    "search_messages_indexed": "Elasticsearch",
    "analytics_entity_facts_synchronized": "ClickHouse",
}


def _metric_label(key):
    """Turn an owner-provided metric key into a readable, translatable label."""

    label = str(key).replace(".", " ").replace("_", " ").replace("-", " ").strip()
    if label:
        label = label[0].upper() + label[1:]
    return _(label)


def _index_breakdown(records):
    """Describe recorded coverage without reconstructing historical writes."""
    records = list(records)
    detailed = [record for record in records if INDEX_DESTINATIONS.keys() <= {entry.key for entry in record.outputs}]
    legacy = len(records) - len(detailed)
    partial = bool(legacy and detailed)
    notices = []
    if partial:
        notices.append(_("Partial destination breakdown: some batches have no recorded details."))
    skipped = sum(record.metadata.get("search_indexing_enabled") is False for record in detailed)
    if skipped:
        notices.append(
            _("Search indexing skipped by configuration.") if skipped == len(records)
            else _("Search indexing skipped for some batches.")
        )
    return partial, " ".join(notices)


def _count_breakdown(item_count, processed_count, failed_count, skipped_count):
    return [
        {"label": _("Input items"), "value": item_count},
        {"label": _("Processed by workflow"), "value": processed_count},
        {"label": _("Skipped"), "value": skipped_count},
        {"label": _("Failed"), "value": failed_count},
    ]


def _stage_state(batch, stage):
    completed = batch.completed_stages or []
    if stage in completed:
        return StageDisplayState.COMPLETED.value
    if batch.current_stage == stage:
        return {
            BatchStatus.RUNNING: StageDisplayState.RUNNING.value,
            BatchStatus.RETRY_WAIT: StageDisplayState.RETRYING.value,
            BatchStatus.FAILED: StageDisplayState.FAILED.value,
            BatchStatus.CANCELLED: StageDisplayState.CANCELLED.value,
        }.get(batch.status, StageDisplayState.PENDING.value)
    if batch.status == BatchStatus.CANCELLED:
        return StageDisplayState.CANCELLED.value
    return StageDisplayState.PENDING.value


def _stage_status(state):
    return {
        StageDisplayState.COMPLETED: {"label": _("Completed"), "tone": "success"},
        StageDisplayState.RUNNING: {"label": _("Running"), "tone": "running"},
        StageDisplayState.RETRYING: {"label": _("Retrying"), "tone": "retry"},
        StageDisplayState.FAILED: {"label": _("Failed"), "tone": "error"},
        StageDisplayState.CANCELLED: {"label": _("Cancelled"), "tone": "muted"},
        StageDisplayState.PENDING: {"label": _("Pending"), "tone": "muted"},
    }.get(state, {"label": _("Pending"), "tone": "muted"})


def _input_label(batch):
    content_type = getattr(batch, "item_content_type", None)
    model = content_type.model_class() if content_type is not None else None
    if model is not None:
        return _(str(model._meta.verbose_name_plural).capitalize())
    return _("records")


def _validated_summary(raw_summary):
    if not isinstance(raw_summary, dict):
        return ProcessingSummary()
    try:
        return ProcessingSummary.model_validate(raw_summary)
    except ValidationError:
        # Invalid or not-yet-converted values are deliberately not interpreted
        # using the previous unversioned format.
        return ProcessingSummary()


def _batch_summary(batch):
    summary = _validated_summary(batch.result_summary)
    completed_stages = getattr(batch, "completed_stages", None) or []
    if TELEGRAM_INDEX_STAGE in completed_stages and TELEGRAM_INDEX_STAGE not in summary.stages:
        summary.stages[TELEGRAM_INDEX_STAGE] = StageSummary()
    return summary


def _display_count(count):
    return _("—") if count is None else count


def _compact_count(count):
    """Make the number on a graph connector short without changing its data."""

    if count is None:
        return _("—")
    if count < 10_000:
        return str(count)
    if count >= 999_950_000_000:
        divisor, suffix = 1_000_000_000_000, "T"
    elif count >= 999_950_000:
        divisor, suffix = 1_000_000_000, "B"
    elif count >= 999_950:
        divisor, suffix = 1_000_000, "M"
    else:
        divisor, suffix = 1_000, "K"
    value = f"{count / divisor:.1f}".rstrip("0").rstrip(".")
    return f"{value}{suffix}"


def _metric_icon(kind, key):
    """Choose a small, stable icon family from the declared metric type."""

    identity = "%s %s" % (kind, key)
    identity = identity.lower()
    if "entit" in identity:
        return "entity"
    if any(token in identity for token in ("url", "link", "domain", "email")):
        return "link"
    if any(token in identity for token in ("message", "text", "record", "document")):
        return "document"
    return "data"


def _metric_identity(metric):
    key = str(metric["key"]).strip().casefold()
    for separator in (".", "-", " "):
        key = key.replace(separator, "_")
    return key, str(metric["kind"]).strip().casefold()


def _present_count_entries(entries):
    return [
        {
            "key": entry.key,
            "label": _metric_label(entry.key),
            "value": _display_count(entry.count),
            "compact_value": _compact_count(entry.count),
            "count": entry.count,
            "kind": entry.kind,
            "unit": entry.unit,
            "icon": _metric_icon(entry.kind, entry.key),
            "destination": INDEX_DESTINATIONS.get(entry.key, ""),
        }
        for entry in entries
    ]


def _present_reason_entries(entries):
    return [
        {
            "key": entry.key,
            "label": _metric_label(entry.key),
            "value": _display_count(entry.count),
            "count": entry.count,
        }
        for entry in entries
    ]


def _aggregate_entries(rows, *, include_stage=False, partial_stages=()):
    grouped = {}
    for stage_key, entry in rows:
        identity = (stage_key, entry.key, entry.kind, entry.unit) if include_stage else (
            entry.key,
            entry.kind,
            entry.unit,
        )
        group = grouped.setdefault(
            identity,
            {
                "key": entry.key,
                "stage_key": stage_key,
                "kind": entry.kind,
                "unit": entry.unit,
                "total": 0,
                "has_unknown": False,
            },
        )
        if entry.count is None:
            group["has_unknown"] = True
        else:
            group["total"] += entry.count

    result = []
    for group in grouped.values():
        count = None if group["has_unknown"] else group["total"]
        label = _metric_label(group["key"])
        partial = group["stage_key"] in partial_stages
        if partial:
            label = _("%(label)s (partial)") % {"label": label}
        result.append(
            {
                "key": group["key"],
                "stage_key": group["stage_key"],
                "stage_label": stage_label(group["stage_key"]) if group["stage_key"] else "",
                "label": label,
                "kind": group["kind"],
                "unit": group["unit"],
                "count": count,
                "value": _display_count(count),
                "compact_value": _compact_count(count),
                "icon": _metric_icon(group["kind"], group["key"]),
                "destination": INDEX_DESTINATIONS.get(group["key"], ""),
                "partial": partial,
            }
        )
    return result


def _result_metrics(summaries):
    rows = []
    index_records = []
    for summary in summaries:
        for stage_key, stage in summary.stages.items():
            rows.extend((stage_key, entry) for entry in stage.outputs)
            if stage_key == TELEGRAM_INDEX_STAGE:
                index_records.append(stage)
    partial, _notice = _index_breakdown(index_records)
    return _aggregate_entries(
        rows, include_stage=True, partial_stages={TELEGRAM_INDEX_STAGE} if partial else (),
    )


def result_metrics_for_summaries(summaries):
    """Aggregate outputs by stage and data kind, never across workflow stages."""
    return _result_metrics(_validated_summary(summary) for summary in summaries)


def result_metrics_for_batches(batches):
    return _result_metrics(_batch_summary(batch) for batch in batches)


def aggregate_stage_summaries(batches):
    """Aggregate each stage's own flow values across its parallel Batches."""

    stage_keys = []
    records = {}
    index_records = []
    for batch in batches:
        workflow = batch.job.workflow if isinstance(batch.job.workflow, list) else []
        summary = _batch_summary(batch)
        index_record = summary.stages.get(TELEGRAM_INDEX_STAGE)
        if index_record is not None:
            index_records.append(index_record)
        for stage_key in workflow:
            if stage_key not in records:
                stage_keys.append(stage_key)
                records[stage_key] = {name: [] for name in ("inputs", "processed", "outputs", "skipped", "failed")}
        for stage_key, stage in summary.stages.items():
            if stage_key not in records:
                stage_keys.append(stage_key)
                records[stage_key] = {name: [] for name in ("inputs", "processed", "outputs", "skipped", "failed")}
            for category in ("inputs", "processed", "outputs"):
                records[stage_key][category].extend((stage_key, entry) for entry in getattr(stage, category))
            for category in ("skipped", "failed"):
                records[stage_key][category].extend((stage_key, entry) for entry in getattr(stage, category))

    partial_index, index_notice = _index_breakdown(index_records)
    result = []
    for stage_key in stage_keys:
        stage_rows = records[stage_key]
        item = {
            "key": stage_key,
            "label": stage_label(stage_key),
            "notice": index_notice if stage_key == TELEGRAM_INDEX_STAGE else "",
        }
        for category in ("inputs", "processed", "outputs"):
            partial_stages = {TELEGRAM_INDEX_STAGE} if partial_index and category == "outputs" else ()
            item[category] = _aggregate_entries(stage_rows[category], partial_stages=partial_stages)
        for category in ("skipped", "failed"):
            item[category] = _aggregate_reasons(stage_rows[category])
        result.append(item)
    return result


def _aggregate_reasons(rows):
    grouped = {}
    for _, reason in rows:
        group = grouped.setdefault(
            reason.key,
            {"key": reason.key, "total": 0, "has_unknown": False},
        )
        if reason.count is None:
            group["has_unknown"] = True
        else:
            group["total"] += reason.count

    return [
        {
            "key": group["key"],
            "label": _metric_label(group["key"]),
            "count": None if group["has_unknown"] else group["total"],
            "value": _display_count(None if group["has_unknown"] else group["total"]),
            "compact_value": _compact_count(None if group["has_unknown"] else group["total"]),
        }
        for group in grouped.values()
    ]


def _stage_results(batch):
    summary = _batch_summary(batch)
    workflow = batch.job.workflow if isinstance(batch.job.workflow, list) else []
    if not workflow:
        workflow = list(summary.stages)
    results = []
    for stage_key in workflow:
        record = summary.stages.get(stage_key)
        state = _stage_state(batch, stage_key)
        attempts = (batch.stage_attempts or {}).get(stage_key, 0)
        if isinstance(attempts, dict):
            attempts = attempts.get("attempt", attempts.get("count", 0))
        try:
            attempts = int(attempts or 0)
        except (TypeError, ValueError):
            attempts = 0
        inputs = _present_count_entries(record.inputs) if record else []
        processed = _present_count_entries(record.processed) if record else []
        outputs = _present_count_entries(record.outputs) if record else []
        skipped = _present_reason_entries(record.skipped) if record else []
        failed = _present_reason_entries(record.failed) if record else []
        notice = ""
        if stage_key == TELEGRAM_INDEX_STAGE:
            _partial, notice = _index_breakdown([record] if record is not None else [])
        results.append(
            {
                "key": stage_key,
                "label": stage_label(stage_key),
                "state": state,
                "status": _stage_status(state),
                "inputs": inputs,
                "processed": processed,
                "outputs": outputs,
                "metrics": outputs,
                "skipped": skipped,
                "failed": failed,
                "excluded_reasons": [
                    {"category": _("Skipped"), **reason} for reason in skipped
                ] + [
                    {"category": _("Failed"), **reason} for reason in failed
                ],
                "input_count": inputs[0]["count"] if inputs else None,
                "input_label": inputs[0]["label"] if inputs else _input_label(batch),
                "processed_count": processed[0]["count"] if processed else None,
                "processed_label": processed[0]["label"] if processed else _("Processed"),
                "failed_count": _reason_total(failed),
                "excluded_count": _reason_total(skipped),
                "metadata": record.metadata if record else {},
                "notice": notice,
                "attempts": attempts,
            }
        )

    # Keep every stage's measurements in its detail. On the graph, represent a
    # matching output/input metric once; if its count changes at the boundary,
    # show that transition on the shared connector instead of repeating its icon.
    for stage in results:
        stage["graph_inputs"] = [
            {**metric, "direction": "input", "context_stage_label": stage["label"]}
            for metric in stage["inputs"]
        ]
        stage["graph_outputs"] = [
            {**metric, "direction": "output", "context_stage_label": stage["label"]}
            for metric in stage["outputs"]
        ]
        if not stage["graph_outputs"]:
            stage["graph_outputs"] = [
                {**metric, "direction": "processed", "context_stage_label": stage["label"]}
                for metric in stage["processed"]
            ]
    for previous, current in zip(results, results[1:]):
        previous_outputs = {}
        for metric in previous["graph_outputs"]:
            previous_outputs.setdefault(_metric_identity(metric), []).append(metric)

        remaining_inputs = []
        for metric in current["graph_inputs"]:
            matches = previous_outputs.get(_metric_identity(metric))
            if matches:
                output = matches.pop(0)
                output["shared_input_stage_label"] = current["label"]
                output["shared_input_count"] = metric["count"]
                output["shared_input_value"] = metric["value"]
                output["shared_input_compact_value"] = metric["compact_value"]
                output["shared_input_unit"] = metric["unit"]
                output["shared_input_same_value"] = (
                    metric["count"] == output["count"] and metric["unit"] == output["unit"]
                )
            else:
                remaining_inputs.append(metric)
        current["graph_inputs"] = remaining_inputs

    for index, stage in enumerate(results):
        connector_metrics = [dict(metric) for metric in stage["graph_outputs"]]
        if index + 1 < len(results):
            connector_metrics.extend(
                dict(metric) for metric in results[index + 1]["graph_inputs"]
            )
        stage["connector_metrics"] = connector_metrics

    return results


def _reason_total(reasons):
    if any(reason["count"] is None for reason in reasons):
        return None
    if not reasons:
        return 0
    return sum(reason["count"] for reason in reasons)


def _progress(job):
    total = job.estimated_batches_total
    if total is None:
        total = job.total_batches
    if not total:
        return None
    completed = min(job.completed_batches, total)
    return round(completed * 100 / total)


def _present_filters(filters):
    if not isinstance(filters, dict):
        return []
    result = []
    for key, value in filters.items():
        if key in HIDDEN_PARAMETER_FIELDS or value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        elif isinstance(value, bool):
            value = _("Yes") if value else _("No")
        result.append(
            {
                "label": PIPELINE_FIELD_LABELS.get(key, _("Additional criterion")),
                "value": value,
            }
        )
    return result


def present_job(job, *, batches=None):
    meta = job_status_meta(job.status)
    return {
        "object": job,
        "id": str(job.pk),
        "label": pipeline_label(job.processor),
        "status": meta,
        "status_code": job.status,
        "progress": _progress(job),
        "processed_items": job.processed_items,
        "failed_items": job.failed_items,
        "skipped_items": job.skipped_items,
        "total_items": job.total_items,
        "total_batches": job.total_batches,
        "completed_batches": job.completed_batches,
        "failed_batches": job.failed_batches,
        "error_recovered": job.status == ProcessingJob.COMPLETED and job.failed_batches == 0,
        "result_metrics": result_metrics_for_batches(batches or []),
        "input_outcome": job.input_outcome,
        "failures_abandoned_at": job.failures_abandoned_at,
        "filters": _present_filters(job.filters),
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "updated_at": job.updated_at,
        "active": job.status in ProcessingJob.ACTIVE_STATUSES,
        "technical": {
            "processor": job.processor,
            "mode": job.mode,
            "workflow": job.workflow,
            "request_token": str(job.request_token) if job.request_token else "",
            "batch_size": job.batch_size,
            "max_inflight_batches": job.max_inflight_batches,
        },
    }


def present_batch(batch):
    meta = batch_status_meta(batch.status)
    attempts = batch.stage_attempts if isinstance(batch.stage_attempts, dict) else {}
    return {
        "object": batch,
        "id": str(batch.pk),
        "ordinal": batch.ordinal,
        "status": meta,
        "status_code": batch.status,
        "stage_results": _stage_results(batch),
        "input_label": _input_label(batch),
        "input_explanation": _(
            "This Batch received %(count)s %(label)s from the previous handoff. "
            "The figures below belong to separate stages and must not be added together."
        ) % {"count": batch.item_count, "label": _input_label(batch)},
        "skipped_count": batch.skipped_count,
        "result_metrics": result_metrics_for_summaries([batch.result_summary]),
        "count_breakdown": _count_breakdown(
            batch.item_count,
            batch.processed_count,
            batch.failed_count,
            batch.skipped_count,
        ),
        "count_explanation": _(
            "Input items are the records handed to this Batch. "
            "Processed is the number accepted by the workflow; the rest "
            "may be skipped or failed. Pipeline-specific metrics "
            "below describe what each stage produced."
        ),
        "item_count": batch.item_count,
        "processed_count": batch.processed_count,
        "failed_count": batch.failed_count,
        "current_stage": (
            _("Workflow completed")
            if batch.status == BatchStatus.COMPLETED
            else stage_label(batch.current_stage)
            if batch.current_stage
            else _("Not started")
        ),
        "completed_stages": [stage_label(value) for value in (batch.completed_stages or [])],
        "attempts": attempts,
        "last_error": batch.last_error,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }
