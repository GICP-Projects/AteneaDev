"""Telegram-owned durable Job composition.

This module owns source selection and pipeline composition for Telegram.  It
does not implement NER: the NER stage keys are supplied by ``app_entity`` and
executed by its tasks. Scheduler only sees opaque processor/stage names.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from itertools import groupby, islice

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from app_base.api import (
    array_tag_filter_kwargs,
    create_advance_filter,
    bulk_add_query_relationships_with_pks,
)
from app_entity.job_keys import NER_EXTRACT_STAGE, NER_STORE_STAGE
from app_entity.services.jobs import validate_ner_batch_size
from app_scheduler.contracts import StageSummary, SummaryCount, SummaryReason
from app_scheduler.enums import ItemProgressState
from app_scheduler.models import ProcessingJob, ProcessingBatch, ProcessingHandoff
from app_scheduler.services.jobs import (
    available_new_batch_slots,
    build_job_fingerprint,
    create_batch_locked,
    create_handoffs_locked,
    create_processing_job,
    dispatch_ready_batches,
    enqueue_source_dispatch,
    set_estimated_batches_total,
    refresh_job_locked,
    stable_fingerprint,
)
from app_telegram.models import MessageItem, RoomItem, SeedItem, TelegramAuth
from app_telegram.enums import (
    SCAN_COMMENT_PARENT_TERMINAL_STATES,
    SCAN_ROOM_TERMINAL_STATES,
)
from app_telegram.job_keys import (
    TELEGRAM_ACCESS_PROCESSOR,
    TELEGRAM_ACCESS_STAGE,
    TELEGRAM_INDEX_MESSAGES_PROCESSOR,
    TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR,
    TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE,
    TELEGRAM_INDEX_ROOMS_STAGE,
    TELEGRAM_INDEX_STAGE,
    TELEGRAM_POPULATE_PROCESSOR,
    TELEGRAM_POPULATE_STAGE,
    TELEGRAM_POPULATE_WORKFLOW,
    TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
    TELEGRAM_ROOM_METADATA_PROCESSOR,
    TELEGRAM_ROOM_METADATA_STAGE,
    TELEGRAM_SCAN_COMMENTS_PROCESSOR,
    TELEGRAM_SCAN_COMMENTS_STAGE,
    TELEGRAM_SCAN_PROCESSOR,
    TELEGRAM_SCAN_STAGE,
)


TELEGRAM_PROCESS_MESSAGES_WORKFLOW = [NER_EXTRACT_STAGE, NER_STORE_STAGE, TELEGRAM_INDEX_STAGE]
COMMENTS_MESSAGES_INPUT_VERSION = "comments_messages_v1"
ROOM_METADATA_SYNC_VERSION = 2


def pending_scan_room_ids(batch: ProcessingBatch) -> list[str]:
    """Return immutable room inputs without a committed checkpoint outcome."""
    progress = (batch.processor_metadata or {}).get("room_progress", {})
    return [
        str(room_id) for room_id in batch.item_ids
        if progress.get(str(room_id), {}).get("state") not in SCAN_ROOM_TERMINAL_STATES
    ]


def scan_rooms_batch_counts(batch: ProcessingBatch) -> tuple[int, int, int, int]:
    """Count scanned, deferred, pending rooms and messages across all attempts.

    A deferred room is a committed outcome (for example a locked/missing room),
    matching the scan's existing deferred-input semantics. Only uncommitted
    rounds remain pending for automatic retry.
    """
    progress = (batch.processor_metadata or {}).get("room_progress", {})
    entries = [progress.get(str(room_id), {}) for room_id in batch.item_ids]
    completed = sum(entry.get("state") == ItemProgressState.COMPLETED for entry in entries)
    deferred = sum(entry.get("state") == ItemProgressState.DEFERRED for entry in entries)
    recovered = sum(int(entry.get("recovered_count", 0)) for entry in entries)
    return completed, deferred, batch.item_count - completed - deferred, recovered


def scan_rooms_stage_summary(batch: ProcessingBatch, *, failed_pending=False) -> StageSummary:
    """Build the scan summary from durable room outcomes, including earlier retries."""
    completed, deferred, pending, recovered = scan_rooms_batch_counts(batch)
    return StageSummary(
        inputs=[SummaryCount(key="rooms", count=batch.item_count, kind="telegram.room", unit="rooms")],
        processed=[SummaryCount(key="rooms_scanned", count=completed, kind="telegram.room", unit="rooms")],
        outputs=[SummaryCount(key="messages_recovered", count=recovered, kind="telegram.message", unit="messages")],
        failed=[SummaryReason(key="scan_failed", count=pending)] if failed_pending and pending else [],
        metadata={"pending_rooms": pending, "deferred_rooms": deferred},
    )


def validate_scan_comments_batch(batch: ProcessingBatch) -> None:
    """Reject a batch unless its immutable inputs are versioned parent messages.

    Inspect the batch type/version and owner; return None, persist nothing, and
    raise ValueError for old room input or an unsupported processor version.
    """
    message_type = ContentType.objects.get_for_model(MessageItem, for_concrete_model=False)
    if (
        batch.job.processor != TELEGRAM_SCAN_COMMENTS_PROCESSOR
        or batch.processor_version != COMMENTS_MESSAGES_INPUT_VERSION
        or batch.item_content_type_id != message_type.pk
    ):
        raise ValueError("Comments batches require MessageItem IDs and comments_messages_v1 input")


def scan_comments_batch_counts(batch: ProcessingBatch) -> tuple[int, int, int, int]:
    """Return completed, skipped, pending parents and recovered comments.

    Read JSON progress only for immutable batch input IDs. Persist nothing;
    absent progress is pending and terminal parents never count as failed.
    """
    progress = (batch.processor_metadata or {}).get("parent_progress", {})
    completed = sum(
        progress.get(parent_id, {}).get("state") == ItemProgressState.COMPLETED
        for parent_id in batch.item_ids
    )
    skipped = sum(
        progress.get(parent_id, {}).get("state") == ItemProgressState.SKIPPED
        for parent_id in batch.item_ids
    )
    recovered = sum(int(progress.get(parent_id, {}).get("recovered_count", 0)) for parent_id in batch.item_ids)
    return completed, skipped, batch.item_count - completed - skipped, recovered


def scan_comments_stage_summary(batch: ProcessingBatch, *, failed_pending=False) -> StageSummary:
    """Build measured parent/comment outcomes from a batch's durable progress.

    Return a StageSummary without writes. ``failed_pending`` marks outstanding
    input as failed after retries are exhausted; confirmed parents are retained.
    """
    completed, skipped, pending, recovered = scan_comments_batch_counts(batch)
    progress = (batch.processor_metadata or {}).get("parent_progress", {})
    skipped_reasons = {}
    failed_reasons = {}
    for parent_id in batch.item_ids:
        parent = progress.get(parent_id, {})
        reason = parent.get("reason") or "extraction_failed"
        if parent.get("state") == ItemProgressState.SKIPPED:
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
        elif failed_pending and parent.get("state") not in SCAN_COMMENT_PARENT_TERMINAL_STATES:
            failed_reasons[reason] = failed_reasons.get(reason, 0) + 1
    return StageSummary(
        inputs=[SummaryCount(key="parent_messages", count=batch.item_count, kind="telegram.message", unit="parent_messages")],
        processed=[SummaryCount(key="parent_messages_scanned", count=completed, kind="telegram.message", unit="parent_messages")],
        outputs=[SummaryCount(key="comments_recovered", count=recovered, kind="telegram.message", unit="comments")],
        skipped=[SummaryReason(key=key, count=count) for key, count in sorted(skipped_reasons.items())],
        failed=[SummaryReason(key=key, count=count) for key, count in sorted(failed_reasons.items())],
        metadata={
            "pending_parents": pending,
            "skipped_parents": skipped,
            "fetched_records": sum(int(progress.get(parent_id, {}).get("fetched_count", 0)) for parent_id in batch.item_ids),
        },
    )


def room_metadata_stage_summary(batch: ProcessingBatch) -> StageSummary:
    """Summarize durable per-Room synchronization outcomes for the Dashboard.

    Parameters
    ----------
    batch : ProcessingBatch
        Batch containing immutable Room IDs and their saved progress.

    Returns
    -------
    StageSummary
        Synchronized, missing, failed and pending Rooms plus document counts
        acknowledged by Elasticsearch. Qdrant point counts are not inferred.
    """
    entries = (batch.processor_metadata or {}).get("room_progress", {})
    selected = [entries.get(str(room_id), {}) for room_id in batch.item_ids]
    completed = sum(entry.get("state") == "completed" for entry in selected)
    skipped = sum(entry.get("state") == "skipped" for entry in selected)
    failed = sum(entry.get("state") == "failed" for entry in selected)
    return StageSummary(
        inputs=[SummaryCount(key="rooms", count=batch.item_count, kind="telegram.room", unit="rooms")],
        processed=[SummaryCount(key="rooms_synchronized", count=completed, kind="telegram.room", unit="rooms")],
        outputs=[SummaryCount(
            key="search_messages_updated",
            count=sum(int(entry.get("search_messages_updated", 0)) for entry in selected),
            kind="telegram.message", unit="messages",
        )],
        skipped=[SummaryReason(key="room_missing", count=skipped)] if skipped else [],
        failed=[SummaryReason(key="metadata_sync_failed", count=failed)] if failed else [],
        metadata={"pending_rooms": batch.item_count - completed - skipped, "failed_rooms": failed, "synchronized_rooms": completed, "skipped_rooms": skipped},
    )


def _positive_setting(name: str) -> int:
    value = int(getattr(settings, name))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _as_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return timezone.make_aware(value) if timezone.is_naive(value) else value
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise ValueError(f"Invalid datetime: {value!r}")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def normalize_message_filters(filters: dict | None) -> dict:
    """Normalize the public MessageItem filter without importing NER code."""

    filters = filters or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")

    def values(name, *aliases):
        value = next((filters.get(key) for key in (name, *aliases) if filters.get(key) is not None), [])
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{name} must be a list")
        return sorted({str(item).strip() for item in value if str(item).strip()})

    tag_match = filters.get("tag_match", "any")
    if tag_match not in {"any", "all"}:
        raise ValueError("tag_match must be 'any' or 'all'")
    return {
        "room": values("room"),
        "tags": values("tags", "tag"),
        "tag_match": tag_match,
        "lang": values("lang"),
        "type": values("type"),
        "is_reply": filters.get("is_reply"),
        "createdat_min": filters.get("createdat_min"),
        "createdat_max": filters.get("createdat_max"),
        "stored_since": filters.get("stored_since"),
    }


def _message_queryset(job: ProcessingJob):
    filters = job.filters
    queryset = MessageItem.objects.filter(
        create_advance_filter(
            and_filter_fields={
                "is_reply": filters.get("is_reply"),
                "created_at__gte": _as_datetime(filters.get("createdat_min")),
                "created_at__lte": _as_datetime(filters.get("createdat_max")),
                "stored_date__gte": _as_datetime(filters.get("stored_since")),
                "lang__in": filters.get("lang", []),
                "media_type__in": filters.get("type", []),
                **array_tag_filter_kwargs(
                    "room__tags",
                    filters.get("tags", []),
                    filters.get("tag_match", "any"),
                ),
            },
            list_filter_fields={
                "room__unique_name__iexact": {"values": filters.get("room", []), "OR": True},
            },
        )
    ).filter(stored_date__isnull=False)
    if job.source_snapshot_at:
        queryset = queryset.filter(stored_date__lte=job.source_snapshot_at)
    cursor = job.source_cursor or {}
    cursor_date = _as_datetime(cursor.get("stored_date"))
    cursor_id = cursor.get("id")
    if cursor_date and cursor_id:
        queryset = queryset.filter(Q(stored_date__gt=cursor_date) | Q(stored_date=cursor_date, id__gt=cursor_id))
    return queryset.order_by("stored_date", "id")


# ======================================================
# =====     SEARCH AND ANALYTICS SYNC JOBS     =====
# ======================================================


def queue_index_sync(item_ids, *, operation="messages", using="default"):
    """Persist a bounded search/analytics projection job for item IDs.

    The job and its batches are written to PostgreSQL in the caller's delete
    transaction. A worker performs the backend writes after commit, so a
    rollback cannot leave a queued projection for a deletion that never happened.

    Parameters
    ----------
    item_ids: iterable
        Message or entity primary keys to project or remove from the search/analytics backends.
    operation: {"messages", "delete_messages", "delete_entities"}
        ``messages`` rebuilds message and entity-fact documents from PostgreSQL;
        the delete operations remove search documents and cancel analytical facts.
    using: str, default="default"
        Database alias. Projection jobs currently share the default scheduler DB.

    Returns
    -------
    ProcessingJob or None
        The durable job, or ``None`` when no IDs were supplied.
    """
    if using != "default":
        raise ValueError("The entity projection uses the default PostgreSQL database")
    ids = list(dict.fromkeys(str(pk) for pk in item_ids))
    if not ids:
        return None
    if operation not in {"messages", "delete_messages", "delete_entities"}:
        raise ValueError("Unknown entity projection operation")
    from app_entity.models import EntityItem

    ModelClass = EntityItem if operation == "delete_entities" else MessageItem
    with transaction.atomic(using=using):
        # Keep the queued work in the same transaction as the ORM deletion.
        job, _ = create_processing_job(
            processor=TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR, mode=ProcessingJob.BACKFILL,
            workflow=[TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE], filters={"operation": operation},
            batch_size=max(1, settings.INDEX_MANUAL_BATCH_SIZE),
            max_inflight_batches=1, deduplicate=False,
            fingerprint=uuid.uuid4().hex,  # Distinct deletion events must not collide with active jobs.
        )
        content_type = ContentType.objects.get_for_model(ModelClass)
        for start in range(0, len(ids), job.batch_size):
            # Scheduler batches bound the IDs carried through Redis and retries.
            create_batch_locked(
                job, item_ids=ids[start:start + job.batch_size],
                item_content_type=content_type,
            )
        job.source_exhausted = True
        job.input_closed_at = timezone.now()
        job.save(update_fields=["source_exhausted", "input_closed_at", "updated_at"])
        return job


def queue_room_metadata_sync(room_ids, *, source_batch_id=None):
    """Persist pending Room input for one synchronization child per origin Job.

    Parameters
    ----------
    room_ids : iterable
        Room primary keys. Rooms without pending metadata are ignored.
    source_batch_id : UUID or str, optional
        Origin Scan/Populate Batch. Its parent lock serializes child creation;
        durable Handoffs feed the scheduler's existing Room batching. Omission
        creates a standalone Job with closed input for direct service callers.

    Returns
    -------
    ProcessingJob or None
        Existing/new child Job, or None when no Room needs synchronization.

    Notes
    -----
    Call inside the source checkpoint transaction so its data and Handoffs commit
    together. Celery publication is scheduled after commit. Batch size and
    capacity come from TELEGRAM_ROOM_METADATA settings.
    """
    ids = list(RoomItem.objects.filter(
        pk__in=set(map(str, room_ids)), metadata_sync_pending_at__isnull=False,
    ).order_by("pk").values_list("pk", flat=True))
    if not ids:
        return None
    size = int(settings.TELEGRAM_ROOM_METADATA_ROOMS_PER_BATCH)
    capacity = int(settings.TELEGRAM_ROOM_METADATA_MAX_INFLIGHT)
    with transaction.atomic():
        source = ProcessingBatch.objects.get(pk=source_batch_id) if source_batch_id else None
        parent = ProcessingJob.objects.select_for_update(no_key=True).get(pk=source.job_id) if source else None
        job = parent.child_jobs.select_for_update(no_key=True).filter(
            processor=TELEGRAM_ROOM_METADATA_PROCESSOR, filters__room_sync_version=ROOM_METADATA_SYNC_VERSION,
        ).first() if parent else None
        if job is None:
            job, _ = create_processing_job(
                processor=TELEGRAM_ROOM_METADATA_PROCESSOR,
                mode=ProcessingJob.LIVE if parent else ProcessingJob.BACKFILL,
                workflow=[TELEGRAM_ROOM_METADATA_STAGE],
                filters={"room_sync_version": ROOM_METADATA_SYNC_VERSION},
                parent_job=parent, batch_size=size, max_inflight_batches=capacity,
                fingerprint=stable_fingerprint({"parent": str(parent.pk), "processor": TELEGRAM_ROOM_METADATA_PROCESSOR}) if parent else uuid.uuid4().hex,
                deduplicate=bool(parent),
            )
        content_type = ContentType.objects.get_for_model(RoomItem)
        if source:
            waiting = {
                room_id for values in job.incoming_handoffs.filter(status=ProcessingHandoff.PENDING)
                .values_list("item_ids", flat=True) for room_id in values
            }
            create_handoffs_locked(
                source, target_job=job, item_content_type=content_type,
                item_ids=[str(room_id) for room_id in ids if str(room_id) not in waiting],
            )
        else:
            for start in range(0, len(ids), size):
                create_batch_locked(
                    job, item_ids=list(map(str, ids[start:start + size])),
                    item_content_type=content_type,
                )
            job.source_exhausted = True
            job.input_closed_at = timezone.now()
            job.save(update_fields=["source_exhausted", "input_closed_at", "updated_at"])
            refresh_job_locked(job)
        return job


# ======================================================
# =====             MANUAL INDEX JOBS              =====
# ======================================================


def create_manual_index_job(
    *, filters: dict | None = None, request_token=None, lifecycle_policy=None
) -> tuple[ProcessingJob, bool]:
    normalized = normalize_message_filters(filters)
    batch_size = _positive_setting("INDEX_MANUAL_BATCH_SIZE")
    workflow = [TELEGRAM_INDEX_STAGE]
    fingerprint = build_job_fingerprint(
        processor=TELEGRAM_INDEX_MESSAGES_PROCESSOR,
        mode=ProcessingJob.BACKFILL,
        workflow=workflow,
        filters=normalized,
    )
    job, created = create_processing_job(
        processor=TELEGRAM_INDEX_MESSAGES_PROCESSOR,
        mode=ProcessingJob.BACKFILL,
        workflow=workflow,
        filters=normalized,
        batch_size=batch_size,
        max_inflight_batches=_positive_setting("INDEX_MAX_INFLIGHT"),
        request_token=request_token,
        source_snapshot_at=timezone.now(),
        fingerprint=fingerprint,
        lifecycle_policy=lifecycle_policy,
    )
    if created:
        set_estimated_batches_total(job, _message_queryset(job).count())
        transaction.on_commit(lambda job_id=job.id: enqueue_source_dispatch(job_id))
    return job, created


def dispatch_index_backfill(job_id) -> list[str]:
    """Owner-side cursor dispatcher for a manual MessageItem index Job."""

    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job_id)
        if job.processor != TELEGRAM_INDEX_MESSAGES_PROCESSOR or job.mode != ProcessingJob.BACKFILL:
            return []
        if job.status != ProcessingJob.RUNNING or job.source_exhausted:
            return []
        slots = available_new_batch_slots(job)
        if not slots:
            return []
        page_size = job.batch_size * slots
        messages = list(_message_queryset(job).only("id", "stored_date")[:page_size])
        now = timezone.now()
        if not messages:
            job.source_exhausted = True
            job.input_closed_at = now
            job.save(update_fields=["source_exhausted", "input_closed_at", "updated_at"])
            refresh_job_locked(job, now)
            return []
        content_type = ContentType.objects.get_for_model(MessageItem, for_concrete_model=False)
        batches = []
        for offset in range(0, len(messages), job.batch_size):
            page = messages[offset:offset + job.batch_size]
            batches.append(
                create_batch_locked(
                    job,
                    item_ids=[str(message.id) for message in page],
                    item_content_type=content_type,
                )
            )
        last = messages[-1]
        job.source_cursor = {"stored_date": last.stored_date.isoformat(), "id": str(last.id)}
        if len(messages) < page_size:
            job.source_exhausted = True
            job.input_closed_at = now
        job.save(update_fields=["source_cursor", "source_exhausted", "input_closed_at", "updated_at"])
        refresh_job_locked(job, now)
        return [str(batch.id) for batch in batches]


def _scan_fingerprint(*, processor: str, filters: dict, options: dict) -> str:
    options = dict(options)
    if not options.get("check_room", False):
        options.pop("check_room", None)
    return stable_fingerprint({"processor": processor, "filters": filters, "options": options})


def _create_scan_jobs(
    *, processor: str, stage: str, filters: dict, options: dict, request_token=None,
    lifecycle_policy=None, mode=ProcessingJob.LIVE, batch_size=None, source_snapshot_at=None,
):
    """Create a scan root and LIVE processing child in the caller's transaction.

    Parameters specify processor/stage, persisted filters/options, lifecycle,
    input mode/size and optional ingestion-time boundary. Return root, child and
    whether created. Existing active equivalents are reused; invalid workflow
    or sizing raises ValueError through the scheduler primitives.
    """
    root, created = create_processing_job(
        processor=processor,
        mode=mode,
        workflow=[stage],
        filters={**filters, "options": options},
        batch_size=_positive_setting("TELEGRAM_SCAN_ROOMS_PER_BATCH") if batch_size is None else batch_size,
        max_inflight_batches=_positive_setting("TELEGRAM_SCAN_MAX_INFLIGHT"),
        request_token=request_token,
        fingerprint=_scan_fingerprint(processor=processor, filters=filters, options=options),
        lifecycle_policy=lifecycle_policy,
        source_snapshot_at=source_snapshot_at,
    )
    if not created:
        return root, root.child_jobs.get(processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR), False
    child, _ = create_processing_job(
        processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
        mode=ProcessingJob.LIVE,
        workflow=TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
        filters={"source": processor, "parent_job": str(root.id)},
        batch_size=validate_ner_batch_size(settings.NER_BATCH_SIZE),
        max_inflight_batches=_positive_setting("NER_EXTRACT_MAX_INFLIGHT"),
        request_token=request_token,
        parent_job=root,
        fingerprint=stable_fingerprint({"parent": str(root.id), "processor": TELEGRAM_PROCESS_MESSAGES_PROCESSOR}),
        deduplicate=False,
    )
    return root, child, True


def create_scan_jobs_from_queryset(
    queryset,
    *,
    filters: dict,
    options: dict,
    request_token=None,
    lifecycle_policy=None,
) -> tuple[ProcessingJob, ProcessingJob, bool]:
    """Create a normal room-scan root and child from a room queryset.

    Persist static room batches grouped by credential, using filters/options,
    token and lifecycle policy. Return root, child and created flag; queue after
    commit. Invalid scheduler configuration or database errors propagate.
    """

    processor = TELEGRAM_SCAN_PROCESSOR
    stage = TELEGRAM_SCAN_STAGE
    room_content_type = ContentType.objects.get_for_model(RoomItem, for_concrete_model=False)
    size = _positive_setting("TELEGRAM_SCAN_ROOMS_PER_BATCH")
    rooms = (
        queryset.filter(access_auth__is_valid=True, access_auth__isnull=False)
        .select_related("access_auth")
        .order_by("access_auth_id", "id")
    )
    with transaction.atomic():
        # Keep the root and its child invisible until their complete static
        # input exists.  A concurrent identical request can then only see a
        # fully constructed Job tree, never a root without its child.
        root, child, created = _create_scan_jobs(
            processor=processor,
            stage=stage,
            filters=filters,
            options=options,
            request_token=request_token,
            lifecycle_policy=lifecycle_policy,
        )
        if not created:
            return root, child, False
        locked_root = ProcessingJob.objects.select_for_update(no_key=True).get(pk=root.id)
        for auth_id, group in groupby(rooms.iterator(chunk_size=size), key=lambda room: room.access_auth_id):
            group = iter(group)
            while ids := [str(room.id) for room in islice(group, size)]:
                create_batch_locked(
                    locked_root,
                    item_ids=ids,
                    item_content_type=room_content_type,
                    resource_key=f"telegram-auth:{auth_id}",
                    processor_metadata={
                        "auth_id": str(auth_id),
                        "options": options,
                        "message_job_id": str(child.id),
                    },
                    schedule_dispatch=False,
                )
        refresh_job_locked(locked_root)
        locked_root.estimated_batches_total = locked_root.batches.count()
        locked_root.save(update_fields=["estimated_batches_total"])
        # The source is static.  Seal it in the same outer transaction so no
        # worker can observe a partially assembled scan Job.
        if locked_root.input_closed_at is None:
            now = timezone.now()
            locked_root.input_closed_at = now
            locked_root.status = ProcessingJob.RUNNING
            locked_root.started_at = now
            locked_root.save(update_fields=["input_closed_at", "status", "started_at", "updated_at"])
        refresh_job_locked(locked_root)
        transaction.on_commit(lambda durable_job_id=locked_root.id: dispatch_ready_batches(durable_job_id))
    return root, child, True


def _scan_comments_source_queryset(job: ProcessingJob, *, apply_cursor=True):
    """Build the snapshotted parent-message source for one cursor phase.

    ``created_at`` filters publication time; ``stored_since`` and the source
    cursor use ingestion time. Null ingestion dates are a separate ID-paged
    phase so legacy parents remain eligible.
    ``apply_cursor=False`` returns both phases for the launch estimate. Return
    a lazy queryset, write nothing, and raise ValueError for malformed dates.
    """
    filters = job.filters
    options = filters.get("options", {})
    message_filters = options.get("message_filters", {}).get("and_filter_fields", {})
    queryset = MessageItem.objects.filter(
        room_id__in=filters.get("room_ids", []),
        is_reply=False,
        is_valid=True,
        room__access_auth__is_valid=True,
        room__access_auth__isnull=False,
        room__is_channel=True,
        room__is_valid=True,
        room__is_private=False,
        room__is_deleted=False,
        room__following=True,
    )
    for field, value in (
        ("created_at__gte", _as_datetime(message_filters.get("created_at__gte"))),
        ("created_at__lte", _as_datetime(message_filters.get("created_at__lte"))),
        ("stored_date__gte", _as_datetime(message_filters.get("stored_date__gte"))),
    ):
        if value is not None:
            queryset = queryset.filter(**{field: value})
    if message_filters.get("lang__in"):
        queryset = queryset.filter(lang__in=message_filters["lang__in"])
    if message_filters.get("media_type__in"):
        queryset = queryset.filter(media_type__in=message_filters["media_type__in"])
    if job.source_snapshot_at:
        queryset = queryset.filter(Q(stored_date__lte=job.source_snapshot_at) | Q(stored_date__isnull=True))
    if not apply_cursor:
        return queryset.order_by("stored_date", "id")
    cursor = job.source_cursor or {}
    if cursor.get("phase", "dated") == "null":
        queryset = queryset.filter(stored_date__isnull=True)
        if cursor.get("id"):
            queryset = queryset.filter(id__gt=cursor["id"])
        return queryset.order_by("id")
    queryset = queryset.filter(stored_date__isnull=False)
    cursor_date = _as_datetime(cursor.get("stored_date"))
    if cursor_date and cursor.get("id"):
        queryset = queryset.filter(
            Q(stored_date__gt=cursor_date) | Q(stored_date=cursor_date, id__gt=cursor["id"])
        )
    return queryset.order_by("stored_date", "id")


def create_scan_comments_jobs(
    *, room_ids: list[str], filters: dict, options: dict, request_token=None,
    lifecycle_policy=None,
) -> tuple[ProcessingJob, ProcessingJob, bool]:
    """Create a cursor-driven comments Job and its message-processing child.

    Parameters persist selected room IDs, public filters, per-parent options,
    token and lifecycle. Return root, child and created flag. The root uses
    BACKFILL source dispatch; its child receives LIVE message handoffs. Store
    the selection estimate in filters and keep job counters for materialized
    input only. Invalid configuration and database errors propagate.
    """
    batch_size = _positive_setting("TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH")
    room_access = list(
        RoomItem.objects.filter(pk__in=room_ids).order_by("id")
        .values_list("id", "access_auth_id", "access_hash")
    )
    root_filters = {
        **filters, "room_ids": sorted(set(map(str, room_ids))),
        "input_version": COMMENTS_MESSAGES_INPUT_VERSION,
        # A repaired/reassigned room must permit a new execution instead of
        # deduplicating against failed batches carrying its previous access.
        "room_access_fingerprint": stable_fingerprint([
            [str(room_id), str(auth_id) if auth_id else None, access_hash]
            for room_id, auth_id, access_hash in room_access
        ]),
    }
    with transaction.atomic():
        root, child, created = _create_scan_jobs(
            processor=TELEGRAM_SCAN_COMMENTS_PROCESSOR,
            stage=TELEGRAM_SCAN_COMMENTS_STAGE,
            mode=ProcessingJob.BACKFILL,
            filters=root_filters,
            options=options,
            batch_size=batch_size,
            request_token=request_token,
            source_snapshot_at=timezone.now(),
            lifecycle_policy=lifecycle_policy,
        )
        if not created:
            return root, child, False
        selected_count = _scan_comments_source_queryset(root, apply_cursor=False).count()
        root.filters = {**root.filters, "selected_items_count": selected_count}
        root.save(update_fields=["filters", "updated_at"])
        set_estimated_batches_total(root, selected_count)
        transaction.on_commit(lambda job_id=root.id: enqueue_source_dispatch(job_id))
    return root, child, True


def dispatch_scan_comments_source(job_id) -> list[str]:
    """Materialize one bounded page of parent-message batches atomically.

    ``job_id`` identifies a versioned comments root. Return new batch IDs or an
    empty list when paused, exhausted or capacity is occupied. The cursor and
    every batch from its page commit together. No next page is generated while
    any prior page batch awaits publication or processing. Invalid source
    versions and database errors propagate.
    """
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job_id)
        if job.processor != TELEGRAM_SCAN_COMMENTS_PROCESSOR or job.mode != ProcessingJob.BACKFILL:
            return []
        if job.filters.get("input_version") != COMMENTS_MESSAGES_INPUT_VERSION:
            raise ValueError("Comments source requires parent-message input version")
        if job.status != ProcessingJob.RUNNING or job.source_exhausted:
            return []
        # Unpublished PENDING batches also occupy this page. Checking only
        # active capacity would let repeated dispatches create unbounded input.
        if job.batches.exclude(status__in=ProcessingBatch.TERMINAL_STATUSES).exists():
            return []
        page_size = job.batch_size * job.max_inflight_batches
        rows = list(
            _scan_comments_source_queryset(job)
            .values("id", "stored_date", "room__access_auth_id")[:page_size]
        )
        now = timezone.now()
        if not rows:
            cursor = job.source_cursor or {}
            if cursor.get("phase", "dated") == "dated":
                job.source_cursor = {"phase": "null"}
                job.save(update_fields=["source_cursor", "updated_at"])
                transaction.on_commit(lambda: enqueue_source_dispatch(job.id))
            else:
                job.source_exhausted = True
                job.input_closed_at = now
                job.save(update_fields=["source_exhausted", "input_closed_at", "updated_at"])
                refresh_job_locked(job, now)
            return []
        content_type = ContentType.objects.get_for_model(MessageItem, for_concrete_model=False)
        # Group by credential before chunking, so every worker authenticates
        # once and can validate every explicit parent against that credential.
        last = rows[-1]
        rows.sort(key=lambda row: str(row["room__access_auth_id"]))
        message_job_id = str(job.child_jobs.only("id").get(processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR).id)
        batch_ids = []
        for auth_id, group in groupby(rows, key=lambda row: row["room__access_auth_id"]):
            grouped = list(group)
            for offset in range(0, len(grouped), job.batch_size):
                chunk = grouped[offset:offset + job.batch_size]
                batch = create_batch_locked(
                    job,
                    item_ids=[str(row["id"]) for row in chunk],
                    item_content_type=content_type,
                    resource_key=f"telegram-auth:{auth_id}",
                    processor_version=COMMENTS_MESSAGES_INPUT_VERSION,
                    processor_metadata={
                        "auth_id": str(auth_id),
                        "options": job.filters.get("options", {}),
                        "message_job_id": message_job_id,
                        "parent_progress": {},
                    },
                    schedule_dispatch=False,
                )
                batch_ids.append(str(batch.id))
        if last["stored_date"] is None:
            job.source_cursor = {"phase": "null", "id": str(last["id"])}
        else:
            job.source_cursor = {
                "phase": "dated", "stored_date": last["stored_date"].isoformat(), "id": str(last["id"])
            }
        job.save(update_fields=["source_cursor", "updated_at"])
        refresh_job_locked(job, now)
        transaction.on_commit(lambda durable_job_id=job.id: dispatch_ready_batches(durable_job_id))
        return batch_ids


def create_populate_job_from_queryset(
    queryset, *, filters: dict, request_token=None, lifecycle_policy=None
) -> tuple[ProcessingJob, bool]:
    """Build static SeedItem batches, one bounded slice per TelegramAuth."""

    workflow = TELEGRAM_POPULATE_WORKFLOW
    fingerprint = build_job_fingerprint(
        processor=TELEGRAM_POPULATE_PROCESSOR,
        mode=ProcessingJob.LIVE,
        workflow=workflow,
        filters=filters,
    )
    auths = list(
        TelegramAuth.objects.filter(is_valid=True, wait_until__lte=timezone.now())
        .order_by("id")
        .values_list("id", flat=True)
    )
    seed_content_type = ContentType.objects.get_for_model(SeedItem, for_concrete_model=False)
    size = _positive_setting("TELEGRAM_POPULATE_SEEDS_PER_BATCH")
    # Populate deliberately handles one bounded slice per credential.  Do not
    # materialise every eligible seed merely to discard the tail for this run.
    seeds = list(queryset.order_by("id").values_list("id", flat=True)[:len(auths) * size])
    with transaction.atomic():
        job, created = create_processing_job(
            processor=TELEGRAM_POPULATE_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=workflow,
            filters=filters,
            batch_size=size,
            max_inflight_batches=_positive_setting("TELEGRAM_POPULATE_MAX_INFLIGHT"),
            request_token=request_token,
            fingerprint=fingerprint,
            lifecycle_policy=lifecycle_policy,
        )
        if not created:
            return job, False
        locked = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job.id)
        for index, auth in enumerate(auths):
            assigned = seeds[index * size:(index + 1) * size]
            if not assigned:
                break
            create_batch_locked(
                locked,
                item_ids=[str(seed_id) for seed_id in assigned],
                item_content_type=seed_content_type,
                resource_key=f"telegram-auth:{auth}",
                processor_metadata={"auth_id": str(auth)},
                recover_on_lease_expiry=False,
                schedule_dispatch=False,
            )
        refresh_job_locked(locked)
        locked.estimated_batches_total = locked.batches.count()
        locked.save(update_fields=["estimated_batches_total"])
        now = timezone.now()
        locked.input_closed_at = now
        locked.status = ProcessingJob.RUNNING
        locked.started_at = now
        locked.save(update_fields=["input_closed_at", "status", "started_at", "updated_at"])
        refresh_job_locked(locked, now)
        transaction.on_commit(lambda durable_job_id=locked.id: dispatch_ready_batches(durable_job_id))
    return job, True


def create_access_job_from_queryset(
    queryset, *, filters: dict, request_token=None, lifecycle_policy=None
) -> tuple[ProcessingJob, bool]:
    """Build a bounded access-hash recovery Job, distributing rooms by auth.

    Telegram limits ResolveUsername/get_input_entity calls to about 200 per
    credential per day. Each available auth therefore receives at most one
    bounded Batch in this Job; rooms beyond current capacity remain eligible
    for a later access run.
    """

    filters = filters or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")

    def values(name):
        value = filters.get(name) or []
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{name} must be a list")
        return sorted({str(item).strip() for item in value if str(item).strip()})

    tag_match = filters.get("tag_match", "any")
    if tag_match not in {"any", "all"}:
        raise ValueError("tag_match must be 'any' or 'all'")

    normalized_filters = {
        "room": values("room"),
        "tags": values("tags"),
        "tag_match": tag_match,
        "lang": values("lang"),
        "is_channel": filters.get("is_channel"),
        "lastup_min": filters.get("lastup_min"),
        "lastup_max": filters.get("lastup_max"),
    }
    batch_size = _positive_setting("TELEGRAM_ACCESS_ROOMS_PER_BATCH")
    if batch_size > 200:
        raise ValueError("TELEGRAM_ACCESS_ROOMS_PER_BATCH cannot exceed Telegram's 200 daily lookups per auth")

    workflow = [TELEGRAM_ACCESS_STAGE]
    fingerprint = stable_fingerprint(
        {
            "processor": TELEGRAM_ACCESS_PROCESSOR,
            "workflow": workflow,
            "filters": normalized_filters,
            "batch_size": batch_size,
        }
    )
    room_content_type = ContentType.objects.get_for_model(RoomItem, for_concrete_model=False)

    with transaction.atomic():
        job, created = create_processing_job(
            processor=TELEGRAM_ACCESS_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=workflow,
            filters=normalized_filters,
            batch_size=batch_size,
            max_inflight_batches=_positive_setting("TELEGRAM_ACCESS_MAX_INFLIGHT"),
            request_token=request_token,
            source_snapshot_at=timezone.now(),
            fingerprint=fingerprint,
            lifecycle_policy=lifecycle_policy,
        )
        if not created:
            return job, False

        locked = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job.id)
        available_auth_ids = list(
            TelegramAuth.objects.filter(
                is_valid=True,
                wait_until__lte=timezone.now(),
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
        room_ids = list(
            queryset.order_by("id")
            .values_list("id", flat=True)[: batch_size * len(available_auth_ids)]
        )

        # Spread work evenly over usable credentials while retaining any
        # remainder instead of dropping it during integer division.
        per_auth, remainder = divmod(len(room_ids), len(available_auth_ids)) if available_auth_ids else (0, 0)
        offset = 0
        for index, auth_id in enumerate(available_auth_ids):
            assigned_count = per_auth + (1 if index < remainder else 0)
            assigned_ids = room_ids[offset : offset + assigned_count]
            offset += assigned_count
            if not assigned_ids:
                continue
            create_batch_locked(
                locked,
                item_ids=[str(room_id) for room_id in assigned_ids],
                item_content_type=room_content_type,
                resource_key=f"telegram-auth:{auth_id}",
                processor_metadata={"auth_id": str(auth_id)},
                schedule_dispatch=False,
            )

        if request_token and room_ids:
            bulk_add_query_relationships_with_pks(
                room_ids,
                room_content_type,
                request_token,
            )

        now = timezone.now()
        refresh_job_locked(locked, now)
        locked.estimated_batches_total = locked.batches.count()
        locked.save(update_fields=["estimated_batches_total"])
        locked.input_closed_at = now
        locked.status = ProcessingJob.RUNNING
        locked.started_at = now
        locked.save(update_fields=["input_closed_at", "status", "started_at", "updated_at"])
        refresh_job_locked(locked, now)
        transaction.on_commit(lambda durable_job_id=locked.id: dispatch_ready_batches(durable_job_id))

    return job, True
