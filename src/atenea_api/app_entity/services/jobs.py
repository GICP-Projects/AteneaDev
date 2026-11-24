"""NER-owned ProcessingJob behaviour.

The scheduler owns generic Job/Batch state and leases. This module owns every
decision that is specific to NER: MessageItem selection, NERState claims,
metadata/versioning and the NER workflow stages.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Iterable

import requests
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from app_base.api import array_tag_filter_kwargs, create_advance_filter
from app_entity.ner_state import NER_INPUT_NORMALIZER_VERSION, build_ner_input_hash
from app_entity.models import NERState
from app_entity.services.ner import NER_CUSTOM_ENTITIES_VERSION
from app_entity.job_keys import (
    NER_EXTRACT_STAGE,
    NER_PROCESSOR,
    NER_STORE_STAGE,
    NER_WORKFLOW,
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
    stable_fingerprint,
)
from app_telegram.models import MessageItem
from app_telegram.job_keys import TELEGRAM_INDEX_STAGE


class NERMetadataError(RuntimeError):
    """API-NER metadata cannot be obtained or trusted for one batch."""


class StaleNERClaimError(RuntimeError):
    """A batch no longer owns all NERState rows it needs to mutate."""


def _as_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return timezone.make_aware(value) if timezone.is_naive(value) else value
    parsed = parse_datetime(str(value))
    if parsed is None:
        parsed_date = parse_date(str(value))
        if parsed_date is not None:
            parsed = datetime.combine(parsed_date, datetime.min.time())
    if parsed is None:
        raise ValueError(f"Invalid datetime value: {value!r}")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _as_iso(value):
    if isinstance(value, datetime):
        return _as_datetime(value).isoformat()
    if isinstance(value, date):
        return timezone.make_aware(datetime.combine(value, datetime.min.time())).isoformat()
    if isinstance(value, str):
        parsed = _as_datetime(value)
        return parsed.isoformat() if parsed else None
    return value


def normalize_ner_filters(filters: dict | None) -> dict:
    """Normalize the public MessageItem filter for a manual NER Job."""

    filters = filters or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")

    def list_value(*names):
        value = next((filters.get(name) for name in names if filters.get(name) is not None), [])
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{names[0]} must be a list")
        return sorted({str(item).strip() for item in value if str(item).strip()})

    tag_match = filters.get("tag_match", "any")
    if tag_match not in {"any", "all"}:
        raise ValueError("tag_match must be 'any' or 'all'")
    return {
        "room": list_value("room"),
        "tags": list_value("tags", "tag"),
        "tag_match": tag_match,
        "lang": list_value("lang"),
        "type": list_value("type"),
        "is_reply": filters.get("is_reply"),
        "createdat_min": _as_iso(filters.get("createdat_min")),
        "createdat_max": _as_iso(filters.get("createdat_max")),
        "stored_since": _as_iso(filters.get("stored_since")),
        "refresh": bool(filters.get("refresh", False)),
    }


def _metadata_url() -> str:
    endpoint = settings.NER_SERVICE.endpoints["metadata"]
    return f"{settings.NER_SERVICE.host}{endpoint.path}"


def fetch_ner_metadata() -> dict:
    headers = {settings.NER_SERVICE.auth.auth_header: settings.NER_SERVICE.auth.api_key}
    try:
        response = requests.get(
            _metadata_url(),
            headers=headers,
            timeout=settings.NER_METADATA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        metadata = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise NERMetadataError(f"Could not retrieve API-NER metadata: {exc}") from exc
    required = {"service_version", "spacy_version", "models", "schema_version"}
    if not isinstance(metadata, dict) or not required.issubset(metadata):
        raise NERMetadataError("API-NER metadata is incomplete")
    if not isinstance(metadata["models"], dict):
        raise NERMetadataError("API-NER metadata has an invalid models value")
    return metadata


def build_ner_batch_metadata(metadata: dict | None = None) -> tuple[str, dict]:
    """Snapshot the NER version for exactly one batch.

    There is intentionally no Job-wide frozen target. The extraction task
    refreshes this value for every batch immediately before calling API-NER, so
    a later batch can record newer metadata without pausing the whole Job.
    """

    components = {
        "api_ner": metadata or fetch_ner_metadata(),
        "custom_entities_version": NER_CUSTOM_ENTITIES_VERSION,
        "input_normalizer_version": NER_INPUT_NORMALIZER_VERSION,
    }
    return stable_fingerprint(components), components


def validate_ner_batch_size(batch_size: int) -> int:
    """Require a positive number of input IDs for each NER Batch."""

    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("batch_size must be an integer") from exc
    if batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    return batch_size


def create_ner_backfill_job(
    *,
    filters: dict | None = None,
    batch_size: int | None = None,
    request_token=None,
    reindex: bool = False,
    lifecycle_policy: dict | None = None,
) -> tuple[ProcessingJob, bool]:
    """Always project NER facts; reindex also rewrites the message document."""

    normalized = normalize_ner_filters(filters)
    normalized["reindex_messages"] = bool(reindex)
    effective_batch_size = validate_ner_batch_size(
        settings.NER_BATCH_SIZE if batch_size is None else batch_size
    )
    workflow = [*NER_WORKFLOW, TELEGRAM_INDEX_STAGE]
    fingerprint = build_job_fingerprint(
        processor=NER_PROCESSOR,
        mode=ProcessingJob.BACKFILL,
        workflow=workflow,
        filters=normalized,
    )
    job, created = create_processing_job(
        processor=NER_PROCESSOR,
        mode=ProcessingJob.BACKFILL,
        workflow=workflow,
        filters=normalized,
        batch_size=effective_batch_size,
        max_inflight_batches=settings.NER_BACKFILL_MAX_INFLIGHT,
        request_token=request_token,
        source_snapshot_at=timezone.now(),
        fingerprint=fingerprint,
        deduplicate=True,
        lifecycle_policy=lifecycle_policy,
    )
    if created:
        set_estimated_batches_total(job, _message_queryset(job).count())
        transaction.on_commit(lambda job_id=job.id: enqueue_source_dispatch(job_id))
    return job, created


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
    ).exclude(annotated_text__isnull=True).exclude(annotated_text__exact="").exclude(lang__exact="")
    queryset = queryset.filter(stored_date__isnull=False, lang__in=settings.NER_SERVICE.languages)
    if job.source_snapshot_at:
        queryset = queryset.filter(stored_date__lte=job.source_snapshot_at)
    cursor = job.source_cursor or {}
    cursor_date = _as_datetime(cursor.get("stored_date"))
    cursor_id = cursor.get("id")
    if cursor_date and cursor_id:
        queryset = queryset.filter(
            Q(stored_date__gt=cursor_date)
            | Q(stored_date=cursor_date, id__gt=uuid.UUID(str(cursor_id)))
        )
    return queryset.order_by("stored_date", "id")


def message_content_type() -> ContentType:
    return ContentType.objects.get_for_model(MessageItem, for_concrete_model=False)


def _locked_states(messages: Iterable[MessageItem]) -> dict:
    messages = list(messages)
    ids = [message.pk for message in messages]
    if not ids:
        return {}
    content_type = message_content_type()
    states = list(
        NERState.objects.select_for_update(of=("self",)).select_related("claimed_batch")
        .filter(content_type=content_type, object_id__in=ids)
        .order_by("object_id")
    )
    known_ids = {state.object_id for state in states}
    missing = [message_id for message_id in ids if message_id not in known_ids]
    if missing:
        NERState.objects.bulk_create(
            [NERState(content_type=content_type, object_id=message_id) for message_id in missing],
            ignore_conflicts=True,
            batch_size=settings.BULK_BATCH_SIZE,
        )
        states = list(
            NERState.objects.select_for_update(of=("self",)).select_related("claimed_batch")
            .filter(content_type=content_type, object_id__in=ids)
            .order_by("object_id")
        )
    return {state.object_id: state for state in states}


def _message_input_hashes(messages: Iterable[MessageItem]) -> dict:
    return {
        message.pk: build_ner_input_hash(
            message.annotated_text,
            message.lang,
            NER_INPUT_NORMALIZER_VERSION,
        )
        for message in messages
    }


def has_ner_input(message: MessageItem) -> bool:
    """Whether a MessageItem has a meaningful input for NER.

    A scan deliberately hands every newly stored message to its child message
    Job so indexation does not depend on NER eligibility.  Media-only,
    deleted, or not-yet-classified messages are therefore an ordinary
    per-item skip for NER, rather than an error that should retry the whole
    durable batch.
    """

    return (
        isinstance(message.annotated_text, str)
        and bool(message.annotated_text.strip())
        # A live Telegram handoff can contain any language, while API-NER has
        # a deliberately finite model set.  Treat an unsupported language as
        # a skip instead of claiming it and recording a misleading completed
        # NER state with no extraction ever having happened.  If support is
        # added later, the message remains eligible for an ordinary backfill.
        and str(message.lang or "").strip() in settings.NER_SERVICE.languages
    )


def _mark_states_without_ner_input_locked(
    states: Iterable[NERState],
    *,
    now,
    error: str = "Message no longer has NER input",
) -> None:
    """Release a claim whose source disappeared or became non-processable.

    Keep ``claimed_batch`` as audit information.  Once that Batch is terminal,
    a future Job can claim the row again if a text/language later appears.
    Initial messages without NER input never create a state at all.
    """

    states = list(states)
    if not states:
        return
    for state in states:
        state.status = NERState.FAILED
        state.lease_until = None
        state.retry_after = None
        state.last_error = error
        state.updated_at = now
    NERState.objects.bulk_update(
        states,
        ["status", "lease_until", "retry_after", "last_error", "updated_at"],
        batch_size=settings.BULK_BATCH_SIZE,
    )


def state_is_claimable(
    state: NERState,
    input_hash: str,
    now,
    *,
    refresh: bool = False,
) -> bool:
    # A durable Batch, rather than a short execution lease, owns the message.
    # This matters for a large scan: it can have thousands of pending batches
    # while only a few are allowed to run. Letting their state lease expire
    # would let another Job process the same message before the scheduler gets
    # to it. Worker-loss recovery redelivers that same Batch through its own
    # lease; cancellation/terminal batches are the only release points.
    if (
        state.claimed_batch_id
        and state.claimed_batch is not None
        and state.claimed_batch.status not in ProcessingBatch.TERMINAL_STATUSES
    ):
        return False
    # This fallback only applies to a malformed/partially migrated state whose
    # batch row is absent. Normal states always have a durable claimed_batch.
    if state.claimed_batch_id is None and state.lease_until and state.lease_until > now:
        return False
    if refresh:
        return True
    if state.status not in {NERState.STORED, NERState.COMPLETED}:
        return True
    # A real processed hash makes text edits an ordinary invalidation signal.
    # Empty hashes are intentionally left alone: they are valid operational
    # state for data baselined before hashes existed, and a corpus-wide refresh
    # remains an explicit user action.
    return bool(state.processed_input_hash and state.processed_input_hash != input_hash)


def _create_claimed_batch(
    job: ProcessingJob,
    claimed: list[tuple[MessageItem, NERState, str]],
    *,
    now,
) -> ProcessingBatch:
    batch = create_batch_locked(
        job,
        item_ids=[str(message.pk) for message, _, _ in claimed],
        item_content_type=message_content_type(),
        now=now,
    )
    states = []
    for _, state, input_hash in claimed:
        state.claimed_input_hash = input_hash
        state.claimed_batch = batch
        state.last_job = job
        state.last_batch = batch
        state.status = NERState.CLAIMED
        # A claim is durable. ``lease_until`` starts when the stage is queued
        # or running and represents only the current execution attempt.
        state.lease_until = None
        state.retry_after = None
        state.attempt_count += 1
        state.last_error = ""
        state.updated_at = now
        states.append(state)
    NERState.objects.bulk_update(
        states,
        [
            "claimed_input_hash", "claimed_batch", "last_job", "last_batch", "status",
            "lease_until", "retry_after", "attempt_count", "last_error", "updated_at",
        ],
        batch_size=settings.BULK_BATCH_SIZE,
    )
    return batch


def _fill_backfill_locked(
    job: ProcessingJob,
    slots: int,
    now,
) -> list[ProcessingBatch]:
    if job.source_exhausted or slots <= 0:
        return []
    candidate_limit = max(job.batch_size * slots * 4, job.batch_size)
    candidates = list(_message_queryset(job)[:candidate_limit])
    if not candidates:
        job.source_exhausted = True
        job.input_closed_at = now
        return []

    input_hashes = _message_input_hashes(candidates)
    states = _locked_states(candidates)
    batches = []
    claimed = []
    last_candidate = None
    blocked_by_active_claim = False

    for message in candidates:
        state = states[message.pk]
        input_hash = input_hashes[message.pk]
        if not state_is_claimable(
            state,
            input_hash,
            now,
            refresh=bool(job.filters.get("refresh")),
        ):
            already_processed = state.status in {NERState.STORED, NERState.COMPLETED}
            if already_processed:
                last_candidate = message
                continue
            blocked_by_active_claim = True
            break
        last_candidate = message
        claimed.append((message, state, input_hash))
        if len(claimed) == job.batch_size:
            batches.append(
                _create_claimed_batch(
                    job,
                    claimed,
                    now=now,
                )
            )
            claimed = []
            if len(batches) >= slots:
                break

    if claimed and len(batches) < slots:
        batches.append(
            _create_claimed_batch(
                job,
                claimed,
                now=now,
            )
        )

    if last_candidate:
        job.source_cursor = {
            "stored_date": last_candidate.stored_date.isoformat(),
            "id": str(last_candidate.pk),
        }
    consumed_candidate_page = last_candidate is not None and last_candidate.pk == candidates[-1].pk
    if len(candidates) < candidate_limit and consumed_candidate_page and not blocked_by_active_claim:
        job.source_exhausted = True
        job.input_closed_at = now
    return batches


def dispatch_ner_backfill(job_id) -> list[str]:
    """Fill the bounded source window for a manual NER-only job."""

    preview = ProcessingJob.objects.filter(pk=job_id).only(
        "processor", "mode", "status", "max_inflight_batches"
    ).first()
    if (
        preview is None
        or preview.processor != NER_PROCESSOR
        or preview.mode != ProcessingJob.BACKFILL
        or preview.status != ProcessingJob.RUNNING
    ):
        return []
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update(no_key=True).get(pk=job_id)
        if (
            job.processor != NER_PROCESSOR
            or job.mode != ProcessingJob.BACKFILL
            or job.status != ProcessingJob.RUNNING
        ):
            return []
        validate_ner_batch_size(job.batch_size)
        slots = available_new_batch_slots(job)
        previous_cursor = dict(job.source_cursor or {})
        batches = _fill_backfill_locked(
            job,
            slots,
            timezone.now(),
        )
        # ``_fill_backfill_locked`` advances the cursor and may seal the
        # source.  Persist those owner-side fields before refreshing the
        # generic lifecycle counters; ``refresh_job_locked`` intentionally
        # does not know that they exist or save them itself.
        job.save(
            update_fields=[
                "source_cursor", "source_exhausted", "input_closed_at", "updated_at",
            ]
        )
        refresh_job_locked(job)
        # A source page can contain only states that are already complete.  It
        # still advances the cursor, but it creates no Batch whose completion
        # would otherwise wake this owner-side dispatcher again.  Continue
        # asynchronously after commit rather than leaving a large backfill
        # stranded on the first such page.  This is generic state semantics;
        # no date or historical-data policy is involved here.
        if not batches and not job.source_exhausted and job.source_cursor != previous_cursor:
            transaction.on_commit(lambda durable_job_id=job.id: enqueue_source_dispatch(durable_job_id))
        return [str(batch.id) for batch in batches]


def batch_item_ids(batch: ProcessingBatch) -> list[uuid.UUID]:
    try:
        return [uuid.UUID(str(value)) for value in batch.item_ids]
    except (ValueError, TypeError, AttributeError) as exc:
        raise StaleNERClaimError(f"Batch {batch.id} has an invalid MessageItem id") from exc


def claimed_batch_item_ids(batch: ProcessingBatch) -> list[uuid.UUID]:
    """Return the portion of a durable batch that NER actually owns.

    A manual NER backfill claims before creating its Batch.  A message batch
    received through a generic ``ProcessingHandoff`` instead claims lazily when
    its extraction stage starts.  In both cases a message already completed (or
    durably owned by another Job) is a normal skipped item, not a corrupted
    batch.
    """

    ids = batch_item_ids(batch)
    owned = set(
        NERState.objects.filter(
            content_type=message_content_type(),
            object_id__in=ids,
            claimed_batch=batch,
        ).values_list("object_id", flat=True)
    )
    return [item_id for item_id in ids if item_id in owned]


def claim_batch_messages_locked(batch: ProcessingBatch, *, now=None, include_skip_reasons=False):
    """Claim eligible MessageItems for a batch while its row is locked.

    This is what lets the generic scheduler materialise a scan handoff without
    importing NER.  The owner claims only when extraction is about to happen,
    so duplicate scan output and a concurrent manual backfill safely converge
    on one durable state owner.
    """

    now = now or timezone.now()
    ids = batch_item_ids(batch)
    messages_by_id = MessageItem.objects.in_bulk(ids)
    existing_ids = [message_id for message_id in ids if message_id in messages_by_id]
    processable_ids = [
        message_id
        for message_id in existing_ids
        if has_ner_input(messages_by_id[message_id])
    ]
    processable_id_set = set(processable_ids)
    unprocessable_ids = [
        message_id
        for message_id in existing_ids
        if message_id not in processable_id_set
    ]
    messages = [messages_by_id[message_id] for message_id in processable_ids]
    states = _locked_states(messages)
    input_hashes = _message_input_hashes(messages)
    claimed = []
    skipped = [message_id for message_id in ids if message_id not in messages_by_id]
    skipped.extend(unprocessable_ids)
    skip_reasons = {}
    if skipped:
        missing = len([message_id for message_id in ids if message_id not in messages_by_id])
        if missing:
            skip_reasons["missing_source_item"] = missing
    if unprocessable_ids:
        skip_reasons["no_ner_input"] = len(unprocessable_ids)

    # A message can lose its text after an earlier delivery of this same batch
    # claimed it.  Do not create states for ordinary media-only messages, but
    # make an existing claim terminally retryable once the batch finishes.
    if unprocessable_ids:
        owned_unprocessable_states = NERState.objects.select_for_update().filter(
            content_type=message_content_type(),
            object_id__in=unprocessable_ids,
            claimed_batch=batch,
        ).order_by("object_id")
        _mark_states_without_ner_input_locked(owned_unprocessable_states, now=now)

    updates = []
    refresh = bool((batch.job.filters or {}).get("refresh", False))

    for message in messages:
        state = states[message.pk]
        input_hash = input_hashes[message.pk]
        if state.claimed_batch_id == batch.id:
            claimed.append(message.pk)
            continue
        if not state_is_claimable(
            state,
            input_hash,
            now,
            refresh=refresh,
        ):
            skipped.append(message.pk)
            if state.claimed_batch_id:
                reason = "claimed_by_another_job"
            elif state.status in {NERState.STORED, NERState.COMPLETED} and not refresh:
                reason = "already_processed"
            else:
                reason = "not_ready"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        state.claimed_input_hash = input_hash
        state.claimed_batch = batch
        state.last_job = batch.job
        state.last_batch = batch
        state.status = NERState.CLAIMED
        state.lease_until = None
        state.retry_after = None
        state.attempt_count += 1
        state.last_error = ""
        state.updated_at = now
        updates.append(state)
        claimed.append(message.pk)

    if updates:
        NERState.objects.bulk_update(
            updates,
            [
                "claimed_input_hash", "claimed_batch", "last_job", "last_batch", "status",
                "lease_until", "retry_after", "attempt_count", "last_error", "updated_at",
            ],
            batch_size=settings.BULK_BATCH_SIZE,
        )
    if include_skip_reasons:
        return claimed, skipped, skip_reasons
    return claimed, skipped


def ensure_batch_claims_current(batch: ProcessingBatch, *, expected_ids: Iterable[uuid.UUID] | None = None) -> None:
    expected = list(expected_ids) if expected_ids is not None else claimed_batch_item_ids(batch)
    if not expected:
        return
    owned = set(
        NERState.objects.filter(
            content_type=message_content_type(),
            object_id__in=expected,
            claimed_batch=batch,
        ).values_list("object_id", flat=True)
    )
    if owned != set(expected):
        raise StaleNERClaimError(f"Batch {batch.id} no longer owns its NER states")


def refresh_batch_metadata_locked(batch: ProcessingBatch, *, version: str, metadata: dict) -> None:
    """Persist the metadata observed immediately before NER extraction.

    This deliberately belongs to the NER owner. The generic scheduler stores
    the opaque fields but never interprets their contents.
    """

    batch.processor_version = version
    batch.processor_metadata = metadata
    batch.save(update_fields=["processor_version", "processor_metadata", "updated_at"])


def update_batch_states(batch: ProcessingBatch, **values) -> None:
    object_ids = values.pop("object_ids", None)
    values["updated_at"] = timezone.now()
    states = NERState.objects.filter(claimed_batch=batch)
    if object_ids is not None:
        states = states.filter(object_id__in=list(object_ids))
    states.update(**values)


def active_batch_state_ids(batch: ProcessingBatch) -> list[uuid.UUID]:
    """Return the claimed rows that belong to the currently retriable work."""

    return list(
        NERState.objects.filter(claimed_batch=batch)
        .exclude(status=NERState.FAILED)
        .values_list("object_id", flat=True)
    )


def prepare_extraction_input_locked(batch: ProcessingBatch):
    ids = claimed_batch_item_ids(batch)
    if not ids:
        return {}, {}, []
    messages = MessageItem.objects.in_bulk(ids)
    states = {
        state.object_id: state
        for state in NERState.objects.select_for_update().filter(
            content_type=message_content_type(),
            object_id__in=ids,
            claimed_batch=batch,
        )
    }
    if len(states) != len(ids):
        raise StaleNERClaimError(f"Batch {batch.id} no longer owns every NER state")

    data = {}
    input_hashes = {}
    changed = []
    messages_with_hash = []
    unprocessable_states = []
    processable_ids = []
    for message_id in ids:
        message = messages.get(message_id)
        state = states[message_id]
        if message is None or not has_ner_input(message):
            unprocessable_states.append(state)
            continue
        input_hash = build_ner_input_hash(
            message.annotated_text,
            message.lang,
            NER_INPUT_NORMALIZER_VERSION,
        )
        if state.claimed_input_hash != input_hash:
            state.claimed_input_hash = input_hash
            state.updated_at = timezone.now()
            changed.append(state)
        data.setdefault(message.lang, []).append({"pk": message.pk, "text": message.annotated_text})
        input_hashes[str(message.pk)] = input_hash
        processable_ids.append(message_id)
        if message.ner_input_hash != input_hash:
            message.ner_input_hash = input_hash
            messages_with_hash.append(message)
    _mark_states_without_ner_input_locked(unprocessable_states, now=timezone.now())
    if changed:
        NERState.objects.bulk_update(changed, ["claimed_input_hash", "updated_at"], batch_size=settings.BULK_BATCH_SIZE)
    if messages_with_hash:
        MessageItem.objects.bulk_update(messages_with_hash, ["ner_input_hash"], batch_size=settings.BULK_BATCH_SIZE)
    return data, input_hashes, processable_ids


def mark_batch_ner_processed_locked(batch: ProcessingBatch, payload: dict, now=None) -> None:
    now = now or timezone.now()
    payload_ids = [uuid.UUID(str(value)) for value in payload.get("message_ids", [])]
    states = list(
        NERState.objects.select_for_update()
        .filter(claimed_batch=batch, object_id__in=payload_ids)
        .order_by("object_id")
    )
    if len(states) != len(payload_ids):
        raise StaleNERClaimError(f"Batch {batch.id} no longer owns every NER state")
    for state in states:
        state.processed_input_hash = payload["input_hashes"].get(str(state.object_id), state.claimed_input_hash)
        state.ner_version = batch.processor_version
        state.processed_at = now
        # NER is complete after storage. A following Telegram index stage is a
        # different concern and must not redefine the NER state.
        state.status = NERState.COMPLETED
        state.lease_until = None
        state.retry_after = None
        state.last_error = ""
        state.updated_at = now
    NERState.objects.bulk_update(
        states,
        [
            "processed_input_hash", "ner_version", "processed_at", "status", "lease_until",
            "retry_after", "last_error", "updated_at",
        ],
        batch_size=settings.BULK_BATCH_SIZE,
    )
