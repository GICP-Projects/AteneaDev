"""Celery tasks for NER work.

Only UUID batch identifiers cross Redis. Text, NER output and NERState remain
in PostgreSQL under the ownership of ``app_entity``.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid

import redis
from celery import shared_task
from django.conf import settings
from django.db import OperationalError, transaction

from app_base.models import GeneralEncoder
from app_entity.enums import NERTaskOutcome
from app_telegram.job_keys import TELEGRAM_INDEX_STAGE
from app_entity.models import NERBatchResult, NERState
from app_entity.services.jobs import (
    NER_EXTRACT_STAGE,
    NER_STORE_STAGE,
    StaleNERClaimError,
    active_batch_state_ids,
    batch_item_ids,
    build_ner_batch_metadata,
    claim_batch_messages_locked,
    dispatch_ner_backfill,
    ensure_batch_claims_current,
    mark_batch_ner_processed_locked,
    prepare_extraction_input_locked,
    refresh_batch_metadata_locked,
    update_batch_states,
)
from app_entity.services.ner import NerExtractionError, extract_ner_data, store_ner_results
from app_scheduler.db import lock_batch_and_job, retry_on_deadlock
from app_scheduler.models import ProcessingBatch
from app_scheduler.contracts import StageSummary, SummaryCount, SummaryReason
from app_scheduler.enums import TaskOutcome
from app_scheduler.services.jobs import (
    begin_batch_stage,
    begin_batch_stage_locked,
    complete_batch_stage_locked,
    ensure_stage_lease_locked,
    fail_batch_stage_locked,
    retry_batch_stage_locked,
)
from app_telegram.models import MessageItem


logger = logging.getLogger(__name__)


class NERHTTPCapacityUnavailable(RuntimeError):
    """Every globally shared API-NER request slot is leased."""


class RedisHTTPLease:
    """A small Redis counting semaphore whose slots expire after worker loss."""

    prefix = "atenea:ner:http-slot"

    def __init__(self):
        self.token = uuid.uuid4().hex
        self.slot = None
        self.client = None

    def acquire(self) -> bool:
        if settings.CELERY_BROKER_URL.startswith("memory://"):
            self.slot = -1
            return True
        try:
            self.client = redis.Redis.from_url(
                settings.CELERY_BROKER_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            for slot in range(settings.NER_HTTP_MAX_CONCURRENCY):
                key = f"{self.prefix}:{slot}"
                if self.client.set(
                    key,
                    self.token,
                    nx=True,
                    ex=settings.NER_EXTRACT_LEASE_SECONDS,
                ):
                    self.slot = slot
                    return True
            return False
        except redis.RedisError as exc:
            raise NERHTTPCapacityUnavailable(f"Redis NER semaphore unavailable: {exc}") from exc

    def release(self) -> None:
        if self.slot is None or self.slot == -1 or self.client is None:
            return
        key = f"{self.prefix}:{self.slot}"
        try:
            self.client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
                1,
                key,
                self.token,
            )
        except redis.RedisError:
            logger.warning("Could not release API-NER slot %s; its lease will expire", self.slot)


def _retry_stage(task, batch_id, stage, exc, *, token: str | None):
    if task.request.retries >= task.max_retries:
        @retry_on_deadlock
        def record_failure():
            batch = lock_batch_and_job(batch_id)
            failed = fail_batch_stage_locked(batch, stage=stage, token=token, error=exc)
            if failed:
                update_batch_states(
                    batch,
                    object_ids=active_batch_state_ids(batch),
                    status=NERState.FAILED,
                    lease_until=None,
                    retry_after=None,
                    last_error=str(exc),
                )

        record_failure()
        return {"batch_id": str(batch_id), "status": TaskOutcome.FAILED.value}

    countdown = random.randint(20, 60)

    @retry_on_deadlock
    def record_retry():
        batch = lock_batch_and_job(batch_id)
        retried = retry_batch_stage_locked(
            batch,
            stage=stage,
            token=token,
            error=exc,
            countdown=countdown,
        )
        if not retried:
            return False
        update_batch_states(
            batch,
            object_ids=active_batch_state_ids(batch),
            status=NERState.RETRY_WAIT,
            lease_until=batch.lease_until,
            retry_after=batch.run_after,
            last_error=str(exc),
        )
        return True

    if not record_retry():
        return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
    logger.warning("NER batch %s failed during %s; retrying in %ss (%s)", batch_id, stage, countdown, exc)
    raise task.retry(exc=exc, countdown=countdown)


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    max_retries=settings.NER_STORE_MAX_RETRIES,
)
def dispatch_ner_backfill_batches(self, job_id):
    """NER-owned source dispatcher for a manual backfill Job."""

    try:
        return dispatch_ner_backfill(job_id)
    except Exception as exc:
        countdown = random.randint(20, 60)
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=countdown)


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.NER_EXTRACT_SOFT_TIME_LIMIT,
    time_limit=settings.NER_EXTRACT_TIME_LIMIT,
    max_retries=settings.NER_EXTRACT_MAX_RETRIES,
)
def extract_ner_batch(self, batch_id):
    """Run the NER extraction stage and persist its temporary output."""

    token = None
    try:
        batch, token = begin_batch_stage(batch_id, NER_EXTRACT_STAGE)
        if token is None:
            return {"batch_id": str(batch_id), "status": batch.status}

        # This happens outside the transaction that owns state/Batch locks.
        # The value persisted below is therefore the metadata of the service
        # used for this actual extraction attempt, not a Job-wide assumption.
        ner_version, ner_metadata = build_ner_batch_metadata()
        @retry_on_deadlock
        def prepare_input():
            with transaction.atomic():
                batch = lock_batch_and_job(batch_id)
                ensure_stage_lease_locked(batch, stage=NER_EXTRACT_STAGE, token=token)
                refresh_batch_metadata_locked(
                    batch,
                    version=ner_version,
                    metadata=ner_metadata,
                )
                claimed_ids, _, skip_reasons = claim_batch_messages_locked(batch, include_skip_reasons=True)
                data, input_hashes, claimed_ids = prepare_extraction_input_locked(batch)
                ensure_batch_claims_current(batch, expected_ids=claimed_ids)
                update_batch_states(
                    batch,
                    object_ids=claimed_ids,
                    status=NERState.EXTRACTING,
                    lease_until=batch.lease_until,
                    retry_after=None,
                    last_error="",
                )
            return batch, claimed_ids, skip_reasons, data, input_hashes

        batch, claimed_ids, skip_reasons, data, input_hashes = prepare_input()

        if data:
            http_lease = RedisHTTPLease()
            if not http_lease.acquire():
                raise NERHTTPCapacityUnavailable("All API-NER HTTP slots are busy")
            try:
                result = extract_ner_data(
                    data,
                    strict=True,
                    timeout_seconds=settings.NER_HTTP_TIMEOUT_SECONDS,
                )
            finally:
                http_lease.release()
        else:
            # A scan may rediscover a message whose state is already complete
            # or owned by another Job.  It remains in the immutable scan batch
            # for indexing/audit, but there is no NER request or DB replacement.
            result = ([], {}, [])

        entities, entity_to_ann, message_ids = result
        payload = json.loads(
            json.dumps(
                {
                    "entities": entities,
                    "entity_to_ann": entity_to_ann,
                    "message_ids": message_ids,
                    "input_hashes": input_hashes,
                },
                cls=GeneralEncoder,
            )
        )
        @retry_on_deadlock
        def persist_extraction():
            with transaction.atomic():
                batch = lock_batch_and_job(batch_id)
                ensure_stage_lease_locked(batch, stage=NER_EXTRACT_STAGE, token=token)
                ensure_batch_claims_current(batch, expected_ids=claimed_ids)
                NERBatchResult.objects.update_or_create(
                    batch=batch,
                    defaults={"payload": payload},
                )
                update_batch_states(
                    batch,
                    object_ids=claimed_ids,
                    status=NERState.EXTRACTED,
                    lease_until=batch.lease_until,
                )
                complete_batch_stage_locked(
                    batch,
                    stage=NER_EXTRACT_STAGE,
                    token=token,
                    processed_count=len(claimed_ids),
                    skipped_count=max(batch.item_count - len(claimed_ids), 0),
                    stage_summary=StageSummary(
                        inputs=[
                            SummaryCount(key="messages", count=batch.item_count, kind="telegram.message", unit="messages")
                        ],
                        processed=[
                            SummaryCount(
                                key="messages_sent_to_ner",
                                count=len(claimed_ids),
                                kind="telegram.message",
                                unit="messages",
                            )
                        ],
                        outputs=[
                            SummaryCount(key="extracted_entities", count=len(entities), kind="entity", unit="entities")
                        ],
                        skipped=[SummaryReason(key=key, count=count) for key, count in skip_reasons.items()],
                    ),
                )

        persist_extraction()
        return {"batch_id": str(batch_id), "status": NERTaskOutcome.EXTRACTED.value}
    except StaleNERClaimError as exc:
        return _retry_stage(self, batch_id, NER_EXTRACT_STAGE, exc, token=token)
    except Exception as exc:
        return _retry_stage(self, batch_id, NER_EXTRACT_STAGE, exc, token=token)


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.NER_STORE_SOFT_TIME_LIMIT,
    time_limit=settings.NER_STORE_TIME_LIMIT,
    max_retries=settings.NER_STORE_MAX_RETRIES,
)
def store_ner_batch(self, batch_id):
    """Persist NER output; recoverable storage failures retry only this stage."""

    task_started = time.time()
    token = None
    try:
        stage_started = time.time()
        @retry_on_deadlock
        def prepare_storage():
            with transaction.atomic():
                batch = lock_batch_and_job(batch_id)
                token = begin_batch_stage_locked(batch, NER_STORE_STAGE)
                if token is None:
                    logger.info(
                        f"NER batch {batch_id} (job: {batch.job_id}, ordinal: {batch.ordinal}) "
                        f"skipped entity.ner.store "
                        f"(status: {batch.status}, current_stage: {batch.current_stage}, "
                        f"lease_until: {batch.lease_until})."
                    )
                    return batch, token, []
                if not NERBatchResult.objects.filter(batch=batch).exists():
                    raise NerExtractionError("Batch has no durable NER result to store")
                result = NERBatchResult.objects.filter(batch=batch).only("payload").first()
                if result is None:
                    raise NerExtractionError("Batch has no durable NER result to store")
                payload_ids = [uuid.UUID(str(value)) for value in result.payload.get("message_ids", [])]
                # Deleting a message also deletes its NERState through GenericRelation.
                existing_ids = list(
                    MessageItem.objects.filter(pk__in=payload_ids).values_list("pk", flat=True)
                )
                ensure_batch_claims_current(batch, expected_ids=existing_ids)
                update_batch_states(
                    batch,
                    object_ids=existing_ids,
                    status=NERState.STORING,
                    lease_until=batch.lease_until,
                    retry_after=None,
                )
            return batch, token, payload_ids

        batch, token, payload_ids = prepare_storage()
        if token is None:
            return {"batch_id": str(batch_id), "status": batch.status}

        logger.info(
            f"NER batch {batch_id} (job: {batch.job_id}, ordinal: {batch.ordinal}, "
            f"workflow: {batch.job.workflow}) entered entity.ner.store "
            f"for {len(payload_ids)} messages "
            f"(stage setup: {(time.time() - stage_started):.3f} secs)."
        )

        transaction_started = time.time()
        @retry_on_deadlock
        def persist_storage():
            with transaction.atomic():
                batch = lock_batch_and_job(batch_id)
                ensure_stage_lease_locked(batch, stage=NER_STORE_STAGE, token=token)
                try:
                    payload = NERBatchResult.objects.select_for_update().get(batch=batch).payload
                except NERBatchResult.DoesNotExist as exc:
                    raise NerExtractionError("Batch lost its durable NER result before storage") from exc
                payload_ids = [uuid.UUID(str(value)) for value in payload.get("message_ids", [])]
                existing_ids = list(
                    MessageItem.objects.filter(pk__in=payload_ids).values_list("pk", flat=True)
                )
                ensure_batch_claims_current(batch, expected_ids=existing_ids)

                if TELEGRAM_INDEX_STAGE not in batch.job.workflow:
                    # Jobs created before projection support also need INDEX after STORE.
                    batch.job.workflow = [*batch.job.workflow, TELEGRAM_INDEX_STAGE]
                    batch.job.filters = {**batch.job.filters, "reindex_messages": False}
                    batch.job.save(update_fields=["workflow", "filters", "updated_at"])

                entity_count = len(payload.get("entities", []))
                annotation_count = sum(
                    len(items)
                    for items in payload.get("entity_to_ann", {}).values()
                )
                logger.info(
                    f"NER batch {batch_id} storage payload loaded "
                    f"(messages: {len(payload_ids)}, unique entities: {entity_count}, "
                    f"annotations: {annotation_count}; load: {(time.time() - transaction_started):.3f} secs)."
                )

                store_started = time.time()
                stored_items = store_ner_results(
                    (payload["entities"], payload["entity_to_ann"], payload["message_ids"]),
                    MessageItem.__name__,
                    MessageItem._meta.app_label,
                    replace_message_ids=payload_ids,
                    atomic=False,
                )
                stored_ids = stored_items.get(MessageItem.__name__, {}).get(
                    "and_filter_fields", {},
                ).get("pk__in", [])
                missing_count = len(payload_ids) - len(stored_ids)
                logger.info(
                    f"NER batch {batch_id} entity and annotation persistence completed "
                    f"(store helper: {(time.time() - store_started):.3f} secs)."
                )

                init_time = time.time()
                mark_batch_ner_processed_locked(batch, {**payload, "message_ids": stored_ids})
                logger.info(
                    f"{len(stored_ids)} NERState items were marked completed "
                    f"(bulk_update: {(time.time() - init_time):.3f} secs)."
                )

                init_time = time.time()
                deleted_count, deleted_by_type = NERBatchResult.objects.filter(batch=batch).delete()
                logger.info(
                    f"{deleted_count} NERBatchResult items were deleted after storage "
                    f"(bulk_delete: {(time.time() - init_time):.3f} secs; "
                    f"breakdown: {deleted_by_type})."
                )

                init_time = time.time()
                complete_batch_stage_locked(
                    batch,
                    stage=NER_STORE_STAGE,
                    token=token,
                    processed_count=len(stored_ids),
                    skipped_count=max(batch.item_count - len(stored_ids), 0),
                    stage_summary=StageSummary(
                        inputs=[
                            SummaryCount(
                                key="messages", count=len(payload_ids), kind="telegram.message", unit="messages"
                            ),
                            SummaryCount(
                                key="extracted_entities", count=entity_count, kind="entity", unit="entities"
                            ),
                        ],
                        processed=[
                            SummaryCount(
                                key="messages", count=len(stored_ids), kind="telegram.message", unit="messages",
                            ),
                        ],
                        skipped=[SummaryReason(key="message_deleted", count=missing_count)] if missing_count else [],
                    ),
                )
                logger.info(
                    f"NER batch {batch_id} store completion state was prepared "
                    f"(stage completion: {(time.time() - init_time):.3f} secs)."
                )

        persist_storage()

        logger.info(
            f"NER batch {batch_id} entity.ner.store transaction committed "
            f"(transaction: {(time.time() - transaction_started):.3f} secs; "
            f"task total: {(time.time() - task_started):.3f} secs)."
        )
        return {"batch_id": str(batch_id), "status": NERTaskOutcome.STORED.value}
    except StaleNERClaimError as exc:
        return _retry_stage(self, batch_id, NER_STORE_STAGE, exc, token=token)
    except OperationalError as exc:
        return _retry_stage(self, batch_id, NER_STORE_STAGE, exc, token=token)
    except Exception as exc:
        return _retry_stage(self, batch_id, NER_STORE_STAGE, exc, token=token)


# Register the app-owned Beat launcher during Celery task autodiscovery.
from app_entity.services.pipelines import ner_extraction_msgs_pipeline  # noqa: E402,F401
