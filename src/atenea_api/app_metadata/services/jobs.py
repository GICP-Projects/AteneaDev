"""Durable ProcessingJob ownership for embeddings and sentiment.

The scheduler persists generic batches and leases, while this module owns
message selection and metadata-domain state.  No vectors or text payloads are
published to Redis: worker tasks receive only a batch UUID.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Iterable

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from app_base.api import array_tag_filter_kwargs, create_advance_filter
from app_metadata.models import EmbeddingsItem, SentimentState
from app_metadata.job_keys import (
    EMBEDDINGS_PROCESSOR,
    EMBEDDINGS_STAGE,
    SENTIMENT_PROCESSOR,
    SENTIMENT_STAGE,
)
from app_scheduler.models import ProcessingBatch, ProcessingJob
from app_scheduler.services.jobs import (
    available_new_batch_slots,
    build_job_fingerprint,
    create_batch_locked,
    create_processing_job,
    enqueue_source_dispatch,
    set_estimated_batches_total,
    refresh_job_locked,
)
from app_telegram.models import MessageItem


def _as_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return timezone.make_aware(value) if timezone.is_naive(value) else value
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise ValueError(f"Invalid datetime: {value!r}")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _iso(value):
    parsed = _as_datetime(value)
    return parsed.isoformat() if parsed else None


def _list_filter(filters, name, *aliases):
    value = next((filters.get(key) for key in (name, *aliases) if filters.get(key) is not None), [])
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    return sorted({str(item).strip() for item in value if str(item).strip()})


def normalize_message_filters(filters: dict | None, *, embedding=False) -> dict:
    """Normalize a MessageItem source filter without depending on HTTP serializers."""

    filters = filters or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    tag_match = filters.get("tag_match", "any")
    if tag_match not in {"any", "all"}:
        raise ValueError("tag_match must be 'any' or 'all'")
    normalized = {
        "room": _list_filter(filters, "room"),
        "tags": _list_filter(filters, "tags", "tag"),
        "tag_match": tag_match,
        "lang": _list_filter(filters, "lang"),
        "type": _list_filter(filters, "type"),
        "is_reply": filters.get("is_reply"),
        "createdat_min": _iso(filters.get("createdat_min")),
        "createdat_max": _iso(filters.get("createdat_max")),
        "stored_since": _iso(filters.get("stored_since")),
        "refresh": bool(filters.get("refresh", False)),
    }
    if embedding:
        slot = filters.get("slot", "default")
        if slot not in {"default", "category"}:
            raise ValueError("slot must be 'default' or 'category'")
        normalized["slot"] = slot
        normalized["instruct"] = str(filters.get("instruct", ""))
    return normalized


def embedding_related_field(slot: str) -> str:
    return "cat_embeddings" if slot == "category" else "embeddings"


def message_content_type():
    return ContentType.objects.get_for_model(MessageItem, for_concrete_model=False)


def _message_queryset(job: ProcessingJob):
    filters = job.filters or {}
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
    ).filter(stored_date__isnull=False).exclude(annotated_text__isnull=True).exclude(annotated_text__exact="")
    if job.source_snapshot_at:
        queryset = queryset.filter(stored_date__lte=job.source_snapshot_at)
    cursor = job.source_cursor or {}
    cursor_date = _as_datetime(cursor.get("stored_date"))
    cursor_id = cursor.get("id")
    if cursor_date and cursor_id:
        queryset = queryset.filter(
            Q(stored_date__gt=cursor_date) | Q(stored_date=cursor_date, id__gt=uuid.UUID(str(cursor_id)))
        )
    return queryset.order_by("stored_date", "id")


def _embeddings_source_queryset(job: ProcessingJob):
    queryset = _message_queryset(job)
    if job.filters.get("refresh"):
        return queryset
    field = embedding_related_field(job.filters.get("slot", "default"))
    stale_before = timezone.now() - timedelta(seconds=settings.EMBEDDINGS_PENDING_TIMEOUT_SECONDS)
    # A completed domain state can deliberately coexist with a Qdrant-pending
    # row (for example, text rejected as too short). Do not turn that known
    # non-work into an endless sequence of manual jobs.
    return queryset.exclude(**{f"{field}__processing_status": EmbeddingsItem.PROCESSING_COMPLETED}).filter(
        Q(**{f"{field}__isnull": True})
        | Q(**{f"{field}__sync_status": EmbeddingsItem.STATUS_FAILED})
        | Q(**{f"{field}__processing_status": EmbeddingsItem.PROCESSING_FAILED})
        | Q(
            **{
                f"{field}__sync_status": EmbeddingsItem.STATUS_PENDING,
                f"{field}__calculated_at__lt": stale_before,
            }
        )
    )


def _sentiment_source_queryset(job: ProcessingJob):
    queryset = _message_queryset(job)
    if job.filters.get("refresh"):
        return queryset
    complete_ids = SentimentState.objects.filter(
        content_type=message_content_type(),
        status=SentimentState.COMPLETED,
    ).values("object_id")
    return queryset.exclude(pk__in=complete_ids)


def _create_job(
    *, processor, stage, filters, batch_size, request_token, queryset_builder,
    lifecycle_policy=None,
):
    fingerprint = build_job_fingerprint(
        processor=processor,
        mode=ProcessingJob.BACKFILL,
        workflow=[stage],
        filters=filters,
    )
    job, created = create_processing_job(
        processor=processor,
        mode=ProcessingJob.BACKFILL,
        workflow=[stage],
        filters=filters,
        batch_size=batch_size,
        max_inflight_batches=settings.METADATA_MAX_INFLIGHT,
        request_token=request_token,
        source_snapshot_at=timezone.now(),
        fingerprint=fingerprint,
        lifecycle_policy=lifecycle_policy,
    )
    if created:
        set_estimated_batches_total(job, queryset_builder(job).count())
        transaction.on_commit(lambda job_id=job.id: enqueue_source_dispatch(job_id))
    return job, created


def create_embeddings_job(*, filters=None, request_token=None, lifecycle_policy=None):
    return _create_job(
        processor=EMBEDDINGS_PROCESSOR,
        stage=EMBEDDINGS_STAGE,
        filters=normalize_message_filters(filters, embedding=True),
        batch_size=settings.EMBEDDINGS_BATCH_SIZE,
        request_token=request_token,
        queryset_builder=_embeddings_source_queryset,
        lifecycle_policy=lifecycle_policy,
    )


def create_sentiment_job(*, filters=None, request_token=None, lifecycle_policy=None):
    return _create_job(
        processor=SENTIMENT_PROCESSOR,
        stage=SENTIMENT_STAGE,
        filters=normalize_message_filters(filters),
        batch_size=settings.SENTIMENT_BATCH_SIZE,
        request_token=request_token,
        queryset_builder=_sentiment_source_queryset,
        lifecycle_policy=lifecycle_policy,
    )


def _dispatch_source(job_id, *, processor, queryset_builder, source_read_size):
    """Materialise at most the current Job capacity from a cursor source."""

    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job_id)
        if (
            job.processor != processor
            or job.mode != ProcessingJob.BACKFILL
            or job.status != ProcessingJob.RUNNING
            or job.source_exhausted
        ):
            return []
        slots = available_new_batch_slots(job)
        if not slots:
            return []
        # Materialise one bounded source page as durable pending Batches.  The
        # generic dispatcher, not Redis, meters how many of them are in flight.
        # Do not advance the cursor again while this page still has unqueued
        # batches; otherwise a fast source dispatcher could build an unbounded
        # backlog every time a single worker finishes.
        if job.batches.filter(
            status=ProcessingBatch.PENDING,
            queued_at__isnull=True,
        ).exists():
            return []
        page_size = max(job.batch_size, int(source_read_size))
        messages = list(queryset_builder(job).only("id", "stored_date")[:page_size])
        now = timezone.now()
        if not messages:
            job.source_exhausted = True
            job.input_closed_at = now
            job.save(update_fields=["source_exhausted", "input_closed_at", "updated_at"])
            refresh_job_locked(job, now)
            return []
        content_type = message_content_type()
        batches = []
        for offset in range(0, len(messages), job.batch_size):
            chunk = messages[offset:offset + job.batch_size]
            batches.append(
                create_batch_locked(
                    job,
                    item_ids=[str(message.pk) for message in chunk],
                    item_content_type=content_type,
                    processor_metadata={
                        "slot": job.filters.get("slot", "default"),
                        "instruct": job.filters.get("instruct", ""),
                    },
                )
            )
        last = messages[-1]
        job.source_cursor = {"stored_date": last.stored_date.isoformat(), "id": str(last.pk)}
        if len(messages) < page_size:
            job.source_exhausted = True
            job.input_closed_at = now
        job.save(update_fields=["source_cursor", "source_exhausted", "input_closed_at", "updated_at"])
        refresh_job_locked(job, now)
        return [str(batch.id) for batch in batches]


def dispatch_embeddings_backfill(job_id):
    return _dispatch_source(
        job_id,
        processor=EMBEDDINGS_PROCESSOR,
        queryset_builder=_embeddings_source_queryset,
        source_read_size=settings.EMBEDDINGS_SOURCE_READ_SIZE,
    )


def dispatch_sentiment_backfill(job_id):
    return _dispatch_source(
        job_id,
        processor=SENTIMENT_PROCESSOR,
        queryset_builder=_sentiment_source_queryset,
        source_read_size=settings.SENTIMENT_SOURCE_READ_SIZE,
    )


def _sentiment_hash(message: MessageItem) -> str:
    value = f"{message.lang or ''}\x1f{message.annotated_text or ''}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _locked_sentiment_states(messages: Iterable[MessageItem]):
    messages = list(messages)
    ids = [message.pk for message in messages]
    if not ids:
        return {}
    content_type = message_content_type()
    states = list(
        SentimentState.objects.select_for_update(of=("self",))
        .select_related("claimed_batch")
        .filter(content_type=content_type, object_id__in=ids)
        .order_by("object_id")
    )
    known = {state.object_id for state in states}
    missing = [item_id for item_id in ids if item_id not in known]
    if missing:
        SentimentState.objects.bulk_create(
            [SentimentState(content_type=content_type, object_id=item_id) for item_id in missing],
            ignore_conflicts=True,
            batch_size=settings.BULK_BATCH_SIZE,
        )
        states = list(
            SentimentState.objects.select_for_update(of=("self",))
            .select_related("claimed_batch")
            .filter(content_type=content_type, object_id__in=ids)
            .order_by("object_id")
        )
    return {state.object_id: state for state in states}


def claim_sentiment_batch_locked(batch: ProcessingBatch):
    """Claim the valid, non-overlapping portion of one sentiment Batch."""

    ids = [uuid.UUID(str(value)) for value in batch.item_ids]
    messages_by_id = MessageItem.objects.in_bulk(ids)
    messages = [messages_by_id[item_id] for item_id in ids if item_id in messages_by_id]
    states = _locked_sentiment_states(messages)
    refresh = bool((batch.job.filters or {}).get("refresh", False))
    claimed, skipped, updates = [], [item_id for item_id in ids if item_id not in messages_by_id], []
    now = timezone.now()
    for message in messages:
        state = states[message.pk]
        input_hash = _sentiment_hash(message)
        active_claim = state.claimed_batch_id and state.claimed_batch and state.claimed_batch.status not in ProcessingBatch.TERMINAL_STATUSES
        if state.claimed_batch_id == batch.id:
            claimed.append(message.pk)
            continue
        if active_claim or (not refresh and state.status == SentimentState.COMPLETED):
            skipped.append(message.pk)
            continue
        state.claimed_input_hash = input_hash
        state.claimed_batch = batch
        state.last_job = batch.job
        state.last_batch = batch
        state.status = SentimentState.CLAIMED
        state.lease_until = None
        state.retry_after = None
        state.attempt_count += 1
        state.last_error = ""
        updates.append(state)
        claimed.append(message.pk)
    if updates:
        SentimentState.objects.bulk_update(
            updates,
            [
                "claimed_input_hash", "claimed_batch", "last_job", "last_batch", "status",
                "lease_until", "retry_after", "attempt_count", "last_error", "updated_at",
            ],
            batch_size=settings.BULK_BATCH_SIZE,
        )
    return claimed, skipped


def update_sentiment_states(batch: ProcessingBatch, **values):
    values["updated_at"] = timezone.now()
    SentimentState.objects.filter(claimed_batch=batch).update(**values)


def complete_sentiment_states_locked(batch: ProcessingBatch, *, succeeded_ids, failed_ids, version):
    now = timezone.now()
    succeeded = {uuid.UUID(str(value)) for value in succeeded_ids}
    failed = {uuid.UUID(str(value)) for value in failed_ids}
    states = list(SentimentState.objects.select_for_update().filter(claimed_batch=batch))
    for state in states:
        if state.object_id in succeeded:
            state.processed_input_hash = state.claimed_input_hash
            state.sentiment_version = version or state.sentiment_version
            state.processed_at = now
            state.status = SentimentState.COMPLETED
            state.last_error = ""
        elif state.object_id in failed:
            state.status = SentimentState.FAILED
            state.last_error = "Sentiment service returned no valid result"
        else:
            # A service response that omitted an ID is an item-level failure,
            # not a reason to leave its durable claim permanently running.
            state.status = SentimentState.FAILED
            state.last_error = "Sentiment service omitted this item"
        state.lease_until = None
        state.retry_after = None
        state.updated_at = now
    SentimentState.objects.bulk_update(
        states,
        [
            "processed_input_hash", "sentiment_version", "processed_at", "status",
            "lease_until", "retry_after", "last_error", "updated_at",
        ],
        batch_size=settings.BULK_BATCH_SIZE,
    )


def update_embedding_processing_state(batch: ProcessingBatch, message_ids, *, status, error="", lease_until=None, retry_after=None):
    """Update EmbeddingsItem ownership for rows claimed by this batch."""

    field = embedding_related_field((batch.processor_metadata or {}).get("slot", "default"))
    embedding_ids = MessageItem.objects.filter(pk__in=message_ids).values_list(f"{field}_id", flat=True)
    EmbeddingsItem.objects.filter(pk__in=embedding_ids, claimed_batch=batch).update(
        claimed_batch=batch,
        last_job=batch.job,
        last_batch=batch,
        processing_status=status,
        lease_until=lease_until,
        retry_after=retry_after,
        processing_error=error,
    )


def claim_embedding_batch_locked(batch: ProcessingBatch):
    """Claim the non-overlapping portion of an embeddings message Batch.

    ``EmbeddingsItem`` is both the vector record and the state ledger.  A
    MessageItem that has no related row yet therefore receives a lightweight
    pending placeholder while its MessageItem row is locked.  That closes the
    otherwise unavoidable race between two differently filtered Jobs that see
    the same unembedded message at the same time.
    """

    try:
        ids = list(dict.fromkeys(uuid.UUID(str(value)) for value in batch.item_ids))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"Batch {batch.id} has an invalid MessageItem id") from exc
    if not ids:
        return [], []

    field = embedding_related_field((batch.processor_metadata or {}).get("slot", "default"))
    messages = list(
        MessageItem.objects.select_for_update()
        .filter(pk__in=ids)
        .only("id", f"{field}_id")
        .order_by("id")
    )
    messages_by_id = {message.pk: message for message in messages}
    ordered_messages = [messages_by_id[item_id] for item_id in ids if item_id in messages_by_id]
    skipped = [item_id for item_id in ids if item_id not in messages_by_id]

    existing_ids = [getattr(message, f"{field}_id") for message in ordered_messages if getattr(message, f"{field}_id")]
    existing = {
        item.pk: item
        for item in EmbeddingsItem.objects.select_for_update()
        .select_related("claimed_batch")
        .filter(pk__in=existing_ids)
        .order_by("id")
    }
    refresh = bool((batch.job.filters or {}).get("refresh", False))
    now = timezone.now()
    new_items = []
    messages_to_link = []
    updates = []
    claimed = []

    for message in ordered_messages:
        embedding_id = getattr(message, f"{field}_id")
        embedding = existing.get(embedding_id)
        if embedding is not None:
            active_claim = (
                embedding.claimed_batch_id
                and embedding.claimed_batch is not None
                and embedding.claimed_batch.status not in ProcessingBatch.TERMINAL_STATUSES
            )
            if embedding.claimed_batch_id == batch.id:
                claimed.append(message.pk)
                continue
            if active_claim or (not refresh and embedding.sync_status == EmbeddingsItem.STATUS_SYNCED):
                skipped.append(message.pk)
                continue
        else:
            embedding = EmbeddingsItem(
                model=settings.OPENAI_EMBEDDINGS.model,
                version="N/A",
                point_id=None,
                sync_status=EmbeddingsItem.STATUS_PENDING,
            )
            # BaseModel has already generated the UUID before bulk_create.
            embedding.point_id = str(embedding.pk)
            new_items.append(embedding)
            setattr(message, f"{field}_id", embedding.pk)
            messages_to_link.append(message)

        embedding.claimed_batch = batch
        embedding.last_job = batch.job
        embedding.last_batch = batch
        embedding.processing_status = EmbeddingsItem.PROCESSING_CLAIMED
        embedding.lease_until = None
        embedding.retry_after = None
        embedding.attempt_count += 1
        embedding.processing_error = ""
        if embedding not in new_items:
            updates.append(embedding)
        claimed.append(message.pk)

    if new_items:
        EmbeddingsItem.objects.bulk_create(new_items, batch_size=settings.BULK_BATCH_SIZE)
    if messages_to_link:
        MessageItem.objects.bulk_update(messages_to_link, [field], batch_size=settings.BULK_BATCH_SIZE)
    if updates:
        EmbeddingsItem.objects.bulk_update(
            updates,
            [
                "claimed_batch", "last_job", "last_batch", "processing_status",
                "lease_until", "retry_after", "attempt_count", "processing_error",
            ],
            batch_size=settings.BULK_BATCH_SIZE,
        )
    return claimed, skipped
