"""Durable Celery stages owned by the metadata application."""

from __future__ import annotations

import random

from celery import shared_task
from django.conf import settings
from django.db import transaction

from app_metadata.models import EmbeddingsItem, SentimentState
from app_metadata.enums import MetadataTaskOutcome
from app_metadata.services.embeddings import calculate_embeddings
from app_metadata.services.jobs import (
    EMBEDDINGS_STAGE,
    SENTIMENT_STAGE,
    claim_embedding_batch_locked,
    claim_sentiment_batch_locked,
    complete_sentiment_states_locked,
    dispatch_embeddings_backfill,
    dispatch_sentiment_backfill,
    embedding_related_field,
    update_embedding_processing_state,
    update_sentiment_states,
)
from app_metadata.services.sentiments import classify_sentiment
from app_scheduler.contracts import StageSummary, SummaryCount, SummaryReason
from app_scheduler.enums import TaskOutcome
from app_scheduler.db import lock_batch_and_job, retry_on_deadlock
from app_scheduler.models import ProcessingBatch
from app_scheduler.services.jobs import (
    begin_batch_stage_locked,
    complete_batch_stage_locked,
    ensure_stage_lease_locked,
    fail_batch_stage_locked,
    retry_batch_stage_locked,
)
from app_telegram.services.room_reconciliation import RoomMetadataWaiting


def _retry_stage(task, batch_id, stage, exc, *, token, state_updater=None):
    waiting = isinstance(exc, RoomMetadataWaiting)
    if not waiting and task.request.retries >= task.max_retries:
        @retry_on_deadlock
        def record_failure():
            batch = lock_batch_and_job(batch_id)
            failed = fail_batch_stage_locked(batch, stage=stage, token=token, error=exc)
            if failed and state_updater:
                state_updater(batch, terminal=True, error=str(exc))
            return failed

        failed = record_failure()
        return {
            "batch_id": str(batch_id),
            "status": TaskOutcome.FAILED.value if failed else TaskOutcome.SUPERSEDED.value,
        }
    countdown = 10 if waiting else random.randint(20, 60)

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
        if state_updater:
            state_updater(batch, terminal=False, error=str(exc))
        return True

    if not record_retry():
        return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
    if waiting:
        return {"batch_id": str(batch_id), "status": TaskOutcome.WAITING_RETRY.value}
    raise task.retry(exc=exc, countdown=countdown)


def _embedding_retry_state(batch, *, terminal, error):
    update_embedding_processing_state(
        batch,
        batch.item_ids,
        status=EmbeddingsItem.PROCESSING_FAILED if terminal else EmbeddingsItem.PROCESSING_RETRY_WAIT,
        error=error,
        lease_until=None if terminal else batch.lease_until,
        retry_after=None if terminal else batch.run_after,
    )


def _sentiment_retry_state(batch, *, terminal, error):
    update_sentiment_states(
        batch,
        status=SentimentState.FAILED if terminal else SentimentState.RETRY_WAIT,
        lease_until=None if terminal else batch.lease_until,
        retry_after=None if terminal else batch.run_after,
        last_error=error,
    )


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.METADATA_SOFT_TIME_LIMIT,
    time_limit=settings.METADATA_TIME_LIMIT,
    max_retries=settings.METADATA_MAX_RETRIES,
)
def dispatch_embeddings_backfill_batches(self, job_id):
    try:
        return dispatch_embeddings_backfill(job_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=random.randint(20, 60))


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.METADATA_SOFT_TIME_LIMIT,
    time_limit=settings.METADATA_TIME_LIMIT,
    max_retries=settings.METADATA_MAX_RETRIES,
)
def dispatch_sentiment_backfill_batches(self, job_id):
    try:
        return dispatch_sentiment_backfill(job_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=random.randint(20, 60))


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.METADATA_SOFT_TIME_LIMIT,
    time_limit=settings.METADATA_TIME_LIMIT,
    max_retries=settings.METADATA_MAX_RETRIES,
)
def embed_processing_batch(self, batch_id):
    """Calculate and persist vectors/Qdrant points for one durable batch."""

    token = None
    try:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            token = begin_batch_stage_locked(batch, EMBEDDINGS_STAGE)
            if token is None:
                return {"batch_id": str(batch_id), "status": batch.status}
            claimed, skipped_before_extract = claim_embedding_batch_locked(batch)
            update_embedding_processing_state(
                batch,
                claimed,
                status=EmbeddingsItem.PROCESSING_RUNNING,
                lease_until=batch.lease_until,
            )
            metadata = batch.processor_metadata or {}
            slot = metadata.get("slot", "default")
            instruct = metadata.get("instruct", "")

        result = calculate_embeddings.run(
            [str(item_id) for item_id in claimed],
            "MessageItem",
            "app_telegram",
            "annotated_text",
            embedding_related_field(slot),
            clean_text=slot == "category",
            instruct=instruct,
        ) or {}
        succeeded = list(result.get("succeeded_ids", []))
        failed = list(result.get("failed_ids", []))
        skipped = list(result.get("skipped_ids", [])) + [str(item_id) for item_id in skipped_before_extract]

        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=EMBEDDINGS_STAGE, token=token)
            update_embedding_processing_state(
                batch,
                succeeded,
                status=EmbeddingsItem.PROCESSING_COMPLETED,
                lease_until=None,
                retry_after=None,
            )
            update_embedding_processing_state(
                batch,
                failed,
                status=EmbeddingsItem.PROCESSING_FAILED,
                error="Embedding provider/Qdrant failed for this item",
                lease_until=None,
                retry_after=None,
            )
            update_embedding_processing_state(
                batch,
                skipped,
                status=EmbeddingsItem.PROCESSING_COMPLETED,
                lease_until=None,
                retry_after=None,
            )
            complete_batch_stage_locked(
                batch,
                stage=EMBEDDINGS_STAGE,
                token=token,
                processed_count=len(succeeded),
                failed_count=len(failed),
                skipped_count=max(batch.item_count - len(succeeded) - len(failed), 0),
                stage_summary=StageSummary(
                    inputs=[
                        SummaryCount(key="messages", count=batch.item_count, kind="telegram.message", unit="messages")
                    ],
                    outputs=[
                        SummaryCount(
                            key="embeddings_generated",
                            count=len(succeeded),
                            kind="metadata.embedding",
                            unit="embeddings",
                        )
                    ],
                    failed=[SummaryReason(key="embedding_processing_failed", count=len(failed))] if failed else [],
                    skipped=[
                        SummaryReason(
                            key="not_processed",
                            count=max(batch.item_count - len(succeeded) - len(failed), 0),
                        )
                    ] if batch.item_count > len(succeeded) + len(failed) else [],
                ),
            )
        return {"batch_id": str(batch_id), "status": MetadataTaskOutcome.EMBEDDED.value}
    except Exception as exc:
        return _retry_stage(
            self,
            batch_id,
            EMBEDDINGS_STAGE,
            exc,
            token=token,
            state_updater=_embedding_retry_state,
        )


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.METADATA_SOFT_TIME_LIMIT,
    time_limit=settings.METADATA_TIME_LIMIT,
    max_retries=settings.METADATA_MAX_RETRIES,
)
def sentiment_processing_batch(self, batch_id):
    """Classify one durable message batch and retain exact partial failures."""

    token = None
    try:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            token = begin_batch_stage_locked(batch, SENTIMENT_STAGE)
            if token is None:
                return {"batch_id": str(batch_id), "status": batch.status}
            claimed, _ = claim_sentiment_batch_locked(batch)
            update_sentiment_states(
                batch,
                status=SentimentState.RUNNING,
                lease_until=batch.lease_until,
                retry_after=None,
                last_error="",
            )

        result = classify_sentiment.run(
            [str(item_id) for item_id in claimed],
            "MessageItem",
            "app_telegram",
            "annotated_text",
            "sentiment",
            "sentiment_model",
            clean_text=True,
        ) or {}
        claimed_ids = {str(item_id) for item_id in claimed}
        succeeded = [item_id for item_id in result.get("succeeded_ids", []) if str(item_id) in claimed_ids]
        failed = sorted(
            (claimed_ids - {str(item_id) for item_id in succeeded})
            | {str(item_id) for item_id in result.get("failed_ids", []) if str(item_id) in claimed_ids}
        )
        version = result.get("version", "")

        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=SENTIMENT_STAGE, token=token)
            complete_sentiment_states_locked(
                batch,
                succeeded_ids=succeeded,
                failed_ids=failed,
                version=version,
            )
            complete_batch_stage_locked(
                batch,
                stage=SENTIMENT_STAGE,
                token=token,
                processed_count=len(succeeded),
                failed_count=len(failed),
                skipped_count=max(batch.item_count - len(succeeded) - len(failed), 0),
                stage_summary=StageSummary(
                    inputs=[
                        SummaryCount(key="messages", count=batch.item_count, kind="telegram.message", unit="messages")
                    ],
                    outputs=[
                        SummaryCount(
                            key="sentiments_classified",
                            count=len(succeeded),
                            kind="metadata.sentiment",
                            unit="classifications",
                        )
                    ],
                    failed=[SummaryReason(key="sentiment_processing_failed", count=len(failed))] if failed else [],
                    skipped=[
                        SummaryReason(
                            key="not_processed",
                            count=max(batch.item_count - len(succeeded) - len(failed), 0),
                        )
                    ] if batch.item_count > len(succeeded) + len(failed) else [],
                    metadata={"model_version": version or None},
                ),
            )
        return {"batch_id": str(batch_id), "status": MetadataTaskOutcome.CLASSIFIED.value}
    except Exception as exc:
        return _retry_stage(
            self,
            batch_id,
            SENTIMENT_STAGE,
            exc,
            token=token,
            state_updater=_sentiment_retry_state,
        )


# Register the app-owned Beat launchers during Celery task autodiscovery.
from app_metadata.services.pipelines import (  # noqa: E402,F401
    embeddings_msgs_pipeline,
    sentiment_classification_msg_pipeline,
)
