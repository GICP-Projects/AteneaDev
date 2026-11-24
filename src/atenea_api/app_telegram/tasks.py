"""Celery stages owned by the Telegram application.

Every durable task receives just a ``ProcessingBatch`` id.  Source IDs,
credentials and downstream Job references are loaded from PostgreSQL.
"""

from __future__ import annotations

import logging
import math
import random

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from telethon.errors import FloodWaitError, AuthKeyError

from app_scheduler.contracts import (
    ProcessingSummary,
    StageSummary,
    SummaryCount,
    SummaryReason,
    empty_processing_summary,
)
from app_scheduler.db import lock_batch_and_job, retry_on_deadlock
from app_scheduler.models import ProcessingBatch
from app_scheduler.enums import BatchStatus, ItemProgressState, JobStatus, TaskOutcome
from app_telegram.enums import SCAN_COMMENT_PARENT_TERMINAL_STATES, TelegramTaskOutcome
from app_scheduler.services.jobs import (
    begin_batch_stage,
    begin_batch_stage_locked,
    complete_batch_stage_locked,
    ensure_stage_lease_locked,
    fail_batch_stage_locked,
    retry_batch_stage_locked,
    StaleStageError,
    refresh_job_locked,
)
from app_telegram.job_keys import TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE, TELEGRAM_ROOM_METADATA_STAGE
from app_telegram.models import RoomItem, SeedItem, TelegramAuth
from app_telegram.services.jobs import (
    TELEGRAM_ACCESS_STAGE,
    TELEGRAM_INDEX_ROOMS_STAGE,
    TELEGRAM_INDEX_STAGE,
    TELEGRAM_POPULATE_STAGE,
    TELEGRAM_SCAN_COMMENTS_STAGE,
    TELEGRAM_SCAN_STAGE,
    dispatch_scan_comments_source,
    dispatch_index_backfill,
    pending_scan_room_ids,
    scan_rooms_batch_counts,
    scan_rooms_stage_summary,
    room_metadata_stage_summary,
    scan_comments_batch_counts,
    scan_comments_stage_summary,
    validate_scan_comments_batch,
)
from app_telegram.services.index import index_message_ids, index_room_ids
from app_telegram.services.room_reconciliation import RoomMetadataWaiting
from app_telegram.services.telegram import (
    access_entities,
    populate_seeds,
    scan_comments,
    scan_rooms,
)


logger = logging.getLogger(__name__)


@shared_task(
    bind=True, ignore_result=True, acks_late=True, task_reject_on_worker_lost=True,
    soft_time_limit=settings.INDEX_SOFT_TIME_LIMIT, time_limit=settings.INDEX_TIME_LIMIT,
    max_retries=settings.INDEX_MAX_RETRIES,
)
def sync_room_metadata_batch(self, batch_id):
    """Project a reconciled room without repeating Telegram or enrichment work."""
    from app_telegram.services.room_reconciliation import project_room_metadata

    token = None
    try:
        batch, token = begin_batch_stage(batch_id, TELEGRAM_ROOM_METADATA_STAGE)
        if token is None:
            return {"batch_id": str(batch_id), "status": batch.status}
        summary = project_room_metadata(batch_id, token)
        if summary.metadata["pending_rooms"]:
            if summary.metadata["failed_rooms"]:
                entries = ProcessingBatch.objects.get(pk=batch_id).processor_metadata["room_progress"]
                raise RuntimeError("; ".join(
                    f"{room_id}: {entry['error']}" for room_id, entry in entries.items()
                    if entry.get("state") == "failed"
                ))
            raise RoomMetadataWaiting("Room metadata synchronization is still pending")
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_ROOM_METADATA_STAGE, token=token)
            complete_batch_stage_locked(
                batch, stage=TELEGRAM_ROOM_METADATA_STAGE, token=token,
                processed_count=summary.metadata["synchronized_rooms"],
                skipped_count=summary.metadata["skipped_rooms"], failed_count=0,
                stage_summary=summary,
            )
        return {"batch_id": str(batch_id), "status": "indexed"}
    except StaleStageError:
        return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
    except Exception as exc:
        remaining = room_metadata_stage_summary(ProcessingBatch.objects.get(pk=batch_id)).metadata["pending_rooms"]
        return _retry_stage(self, batch_id, TELEGRAM_ROOM_METADATA_STAGE, exc, token=token, failed_count=remaining)


def _retry_stage(task, batch_id, stage, exc, *, token, max_retries=None, failed_count=None, countdown=None):
    """Retry or fail a leased stage, optionally counting only failed pending input.

    Parameters identify the Celery task, durable batch/stage, error and lease;
    optional retry cap/countdown override its defaults. Persist retry/failure
    state and return a terminal status, or raise Celery Retry. Superseded
    executions return without mutating another lease's progress.
    """
    if isinstance(exc, RoomMetadataWaiting):
        return _defer_stage(batch_id, stage, exc, token=token, countdown=10)
    max_retries = task.max_retries if max_retries is None else max_retries
    if task.request.retries >= max_retries:
        @retry_on_deadlock
        def record_failure():
            batch = lock_batch_and_job(batch_id)
            return fail_batch_stage_locked(batch, stage=stage, token=token, error=exc, failed_count=failed_count)

        failed = record_failure()
        return {
            "batch_id": str(batch_id),
            "status": TaskOutcome.FAILED.value if failed else TaskOutcome.SUPERSEDED.value,
        }
    countdown = random.randint(20, 60) if countdown is None else countdown

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
        if retried:
            refresh_job_locked(batch.job)
        return retried

    retried = record_retry()
    if not retried:
        return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
    raise task.retry(exc=exc, countdown=countdown)


@retry_on_deadlock
def _defer_stage(batch_id, stage, exc, *, token, countdown):
    """Persist a credential wait for scheduler recovery without Celery retries."""
    with transaction.atomic():
        batch = lock_batch_and_job(batch_id)
        deferred = retry_batch_stage_locked(
            batch,
            stage=stage,
            token=token,
            error=exc,
            countdown=countdown,
        )
        if deferred:
            refresh_job_locked(batch.job)
    if not deferred:
        return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
    # Scheduler reconciliation republishes this durable RETRY_WAIT batch when
    # run_after is reached. Returning acknowledges this delivery without
    # incrementing Celery's bounded retry counter.
    return {"batch_id": str(batch_id), "status": TaskOutcome.WAITING_RETRY.value}


@retry_on_deadlock
def _defer_telegram_floodwait(batch_id, stage, exc, *, token, margin_seconds=60):
    """Persist a credential FloodWait and schedule the leased Batch after it.

    Return ``None`` when the Batch has no usable credential metadata, allowing
    the caller to use its normal error retry path. A stale delivery returns a
    superseded status without changing Batch state.
    """
    now = timezone.now()
    telegram_wait_until = now + timezone.timedelta(seconds=max(int(exc.seconds), 0))
    with transaction.atomic():
        batch = lock_batch_and_job(batch_id)
        try:
            ensure_stage_lease_locked(batch, stage=stage, token=token)
        except StaleStageError:
            return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}

        auth_id = (batch.processor_metadata or {}).get("auth_id")
        if not auth_id:
            return None
        auth = TelegramAuth.objects.select_for_update().filter(pk=auth_id).first()
        if auth is None:
            return None

        auth.wait_until = max(auth.wait_until, telegram_wait_until)
        auth.save(update_fields=["wait_until"])
        retry_base = timezone.now()
        retry_at = max(auth.wait_until, retry_base) + timezone.timedelta(seconds=margin_seconds)
        countdown = max(1, math.ceil((retry_at - retry_base).total_seconds()))
        deferred = retry_batch_stage_locked(
            batch,
            stage=stage,
            token=token,
            error=exc,
            countdown=countdown,
        )
        if deferred:
            refresh_job_locked(batch.job)

    if not deferred:
        return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
    logger.info(
        "Telegram FloodWait deferred Batch. batch_id=%s auth_id=%s telegram_seconds=%s "
        "auth_wait_until=%s retry_at=%s",
        batch_id,
        auth_id,
        exc.seconds,
        auth.wait_until.isoformat(),
        retry_at.isoformat(),
    )
    return {"batch_id": str(batch_id), "status": TaskOutcome.WAITING_RETRY.value}


def _auth_payload(batch: ProcessingBatch) -> dict:
    auth_id = (batch.processor_metadata or {}).get("auth_id")
    if not auth_id:
        raise RuntimeError("Telegram batch has no auth_id")
    auth = TelegramAuth.objects.get(pk=auth_id)
    return {
        "pk": auth.pk,
        "name": auth.name,
        "api_id": auth.api_id,
        "api_hash": auth.api_hash,
        "session": auth.session,
    }


def _release_paused_batch_locked(batch: ProcessingBatch) -> None:
    """Release a leased scan batch for the operator's later resume.

    The caller owns the batch/Job locks and has validated the lease. Clear queue
    and lease fields, retain checkpoint progress, refresh Job counters and return
    None. Database errors propagate within the caller's transaction.
    """
    batch.status = BatchStatus.PENDING.value
    batch.lease_token = ""
    batch.lease_until = None
    batch.queued_at = None
    batch.run_after = None
    batch.save(update_fields=["status", "lease_token", "lease_until", "queued_at", "run_after", "updated_at"])
    refresh_job_locked(batch.job)


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.TELEGRAM_SCAN_SOFT_TIME_LIMIT,
    time_limit=settings.TELEGRAM_SCAN_TIME_LIMIT,
    max_retries=settings.TELEGRAM_SCAN_MAX_RETRIES,
)
def scan_rooms_processing_batch(self, batch_id):
    """Resume only uncommitted room rounds, preserving earlier checkpoint counts."""
    token = None

    def complete_locked(batch):
        completed, deferred, _, _ = scan_rooms_batch_counts(batch)
        complete_batch_stage_locked(
            batch, stage=TELEGRAM_SCAN_STAGE, token=token,
            processed_count=completed, deferred_count=deferred, failed_count=0,
            stage_summary=scan_rooms_stage_summary(batch),
        )

    try:
        batch, token = begin_batch_stage(batch_id, TELEGRAM_SCAN_STAGE)
        if token is None:
            return {"batch_id": str(batch_id), "status": batch.status}
        # Commit lease acquisition before validating credentials or scanning.
        metadata = dict(batch.processor_metadata or {})
        options = metadata.get("options", {})
        pending_ids = pending_scan_room_ids(batch)
        if pending_ids:
            auth = TelegramAuth.objects.get(pk=metadata.get("auth_id"))
            if not auth.is_valid:
                raise RuntimeError("Room scan credential is invalid")
            if auth.wait_until > timezone.now():
                countdown = max(1, math.ceil(
                    (auth.wait_until + timezone.timedelta(minutes=1) - timezone.now()).total_seconds()
                ))
                return _defer_stage(
                    batch_id, TELEGRAM_SCAN_STAGE, f"Credential waits until {auth.wait_until.isoformat()}",
                    token=token, countdown=countdown,
                )
            scan_rooms.run(
                pending_ids, _auth_payload(batch),
                max_msgs=options.get("max_msgs", 2500),
                update_existing_users=options.get("update_existing_users", False),
                check_room=options.get("check_room", False),
                token=batch.job.request_token,
                processing_handoff_target_job_id=metadata.get("message_job_id"),
                source_batch_id=batch.id, lease_token=token,
            )

        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_SCAN_STAGE, token=token)
            _, _, pending, _ = scan_rooms_batch_counts(batch)
            if pending and batch.job.status == JobStatus.PAUSED:
                _release_paused_batch_locked(batch)
                return {"batch_id": str(batch_id), "status": TaskOutcome.PAUSED.value}
            if pending:
                raise RuntimeError(f"{pending} rooms have no committed checkpoint")
            complete_locked(batch)
        return {"batch_id": str(batch_id), "status": TelegramTaskOutcome.SCANNED.value}
    except StaleStageError:
        return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
    except Exception as exc:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            try:
                ensure_stage_lease_locked(batch, stage=TELEGRAM_SCAN_STAGE, token=token)
            except StaleStageError:
                return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
            _, _, pending, _ = scan_rooms_batch_counts(batch)
            if not pending:
                # Client shutdown/on_commit callbacks can fail after the final
                # checkpoint. All input is durable; finish without replaying it.
                complete_locked(batch)
                return {"batch_id": str(batch_id), "status": TelegramTaskOutcome.SCANNED.value}
            if batch.job.status == JobStatus.PAUSED and not isinstance(exc, FloodWaitError):
                _release_paused_batch_locked(batch)
                return {"batch_id": str(batch_id), "status": TaskOutcome.PAUSED.value}
            exhausted = not isinstance(exc, FloodWaitError) and self.request.retries >= self.max_retries
            summary = ProcessingSummary.model_validate(
                empty_processing_summary() if batch.result_summary is None else batch.result_summary
            )
            summary.stages[TELEGRAM_SCAN_STAGE] = scan_rooms_stage_summary(batch, failed_pending=exhausted)
            batch.result_summary = summary.model_dump(mode="json")
            batch.save(update_fields=["result_summary", "updated_at"])
        if isinstance(exc, FloodWaitError):
            deferred = _defer_telegram_floodwait(batch_id, TELEGRAM_SCAN_STAGE, exc, token=token)
            if deferred is not None:
                return deferred
        return _retry_stage(self, batch_id, TELEGRAM_SCAN_STAGE, exc, token=token, failed_count=pending)


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.TELEGRAM_SCAN_SOFT_TIME_LIMIT,
    time_limit=settings.TELEGRAM_SCAN_TIME_LIMIT,
    max_retries=settings.TELEGRAM_SCAN_MAX_RETRIES,
)
def scan_comments_processing_batch(self, batch_id):
    """Run a message-input comments batch, resuming only outstanding parents.

    ``batch_id`` supplies immutable inputs and credential metadata. Return its
    status; persist per-parent progress via scan_comments and complete the stage
    only when every parent is terminal. Transients retry with existing limits;
    credential waits defer durably without consuming that retry budget.
    Exhaustion fails only pending parents.
    Unsupported room/version inputs fail visibly without reinterpretation.
    """
    token = None
    invalid_input = False
    try:
        batch, token = begin_batch_stage(batch_id, TELEGRAM_SCAN_COMMENTS_STAGE)
        if token is None:
            return {"batch_id": str(batch_id), "status": batch.status}
        # Acquire the lease durably before credential/version validation can
        # fail, so an exception never rolls back the token used for retrying.
        try:
            validate_scan_comments_batch(batch)
        except ValueError:
            invalid_input = True
            raise
        metadata = dict(batch.processor_metadata or {})
        options = metadata.get("options", {})
        progress = metadata.get("parent_progress", {})
        pending_ids = [
            parent_id for parent_id in batch.item_ids
            if progress.get(parent_id, {}).get("state") not in SCAN_COMMENT_PARENT_TERMINAL_STATES
        ]
        if pending_ids:
            auth = TelegramAuth.objects.get(pk=metadata.get("auth_id"))
            if not auth.is_valid:
                raise RuntimeError("Comments credential is invalid")
            if auth.wait_until > timezone.now():
                raise RuntimeError(f"Comments credential waits until {auth.wait_until.isoformat()}")
            scan_comments.run(
                pending_ids,
                _auth_payload(batch),
                max_msgs=options.get("max_msgs", 2500),
                token=batch.job.request_token,
                processing_handoff_target_job_id=metadata.get("message_job_id"),
                source_batch_id=batch.id,
                lease_token=token,
            )

        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_SCAN_COMMENTS_STAGE, token=token)
            completed, skipped, pending, _ = scan_comments_batch_counts(batch)
            if pending and batch.job.status == JobStatus.PAUSED:
                # The in-flight parent has committed. Release the batch for
                # resume without spending a retry or starting another parent.
                _release_paused_batch_locked(batch)
                return {"batch_id": str(batch_id), "status": TaskOutcome.PAUSED.value}
            if pending:
                reasons = sorted({
                    entry.get("reason", ItemProgressState.PENDING.value)
                    for entry in batch.processor_metadata.get("parent_progress", {}).values()
                    if entry.get("state") not in SCAN_COMMENT_PARENT_TERMINAL_STATES
                })
                raise RuntimeError(f"{pending} comment parents remain pending: {', '.join(reasons) or 'interrupted'}")
            complete_batch_stage_locked(
                batch,
                stage=TELEGRAM_SCAN_COMMENTS_STAGE,
                token=token,
                processed_count=completed,
                skipped_count=skipped,
                failed_count=0,
                deferred_count=0,
                stage_summary=scan_comments_stage_summary(batch),
            )
        return {"batch_id": str(batch_id), "status": TelegramTaskOutcome.COMMENTS_SCANNED.value}
    except StaleStageError:
        return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
    except Exception as exc:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            try:
                ensure_stage_lease_locked(batch, stage=TELEGRAM_SCAN_COMMENTS_STAGE, token=token)
            except StaleStageError:
                return {"batch_id": str(batch_id), "status": TaskOutcome.SUPERSEDED.value}
            completed, skipped, pending, _ = scan_comments_batch_counts(batch)
            if not invalid_input and not pending:
                # Client shutdown/after-commit coordination can fail after all
                # parent units are durable. No input remains to retry or fail.
                complete_batch_stage_locked(
                    batch, stage=TELEGRAM_SCAN_COMMENTS_STAGE, token=token,
                    processed_count=completed, skipped_count=skipped,
                    failed_count=0, deferred_count=0,
                    stage_summary=scan_comments_stage_summary(batch),
                )
                return {"batch_id": str(batch_id), "status": TelegramTaskOutcome.COMMENTS_SCANNED.value}
            if not invalid_input and batch.job.status == JobStatus.PAUSED:
                _release_paused_batch_locked(batch)
                return {"batch_id": str(batch_id), "status": TaskOutcome.PAUSED.value}
            auth = TelegramAuth.objects.filter(pk=batch.processor_metadata.get("auth_id")).first()
            if auth and isinstance(exc, FloodWaitError):
                # Client startup can FloodWait before any parent transaction
                # exists. Persist that credential delay under the stage lease.
                from django.db.models import Value
                from django.db.models.functions import Greatest
                wait_until = timezone.now() + timezone.timedelta(seconds=exc.seconds)
                TelegramAuth.objects.filter(pk=auth.id).update(wait_until=Greatest("wait_until", Value(wait_until)))
                auth.wait_until = max(auth.wait_until, wait_until)
            elif auth and isinstance(exc, AuthKeyError):
                TelegramAuth.objects.filter(pk=auth.id).update(is_valid=False)
                auth.is_valid = False
            waits = [auth.wait_until] if auth else []
            for entry in batch.processor_metadata.get("parent_progress", {}).values():
                if entry.get("state") == ItemProgressState.PENDING and entry.get("wait_until"):
                    wait = parse_datetime(entry["wait_until"])
                    if wait:
                        waits.append(wait)
            now = timezone.now()
            latest_wait_until = max(waits) if waits else None
            credential_wait = bool(
                not invalid_input
                and auth
                and auth.is_valid
                and not isinstance(exc, AuthKeyError)
                and (
                    isinstance(exc, FloodWaitError)
                    or (latest_wait_until is not None and latest_wait_until > now)
                )
            )
            exhausted = invalid_input or (
                not credential_wait and self.request.retries >= self.max_retries
            )
            summary = ProcessingSummary.model_validate(
                empty_processing_summary() if batch.result_summary is None else batch.result_summary
            )
            summary.stages[TELEGRAM_SCAN_COMMENTS_STAGE] = scan_comments_stage_summary(batch, failed_pending=exhausted)
            batch.result_summary = summary.model_dump(mode="json")
            batch.save(update_fields=["result_summary", "updated_at"])
            if credential_wait:
                # Leave one full minute after Telegram's deadline before
                # allowing the scheduler to send this credential another request.
                wait_base = max(latest_wait_until or now, now)
                retry_at = wait_base + timezone.timedelta(minutes=1)
                countdown = max(1, math.ceil((retry_at - now).total_seconds()))
            else:
                countdown = None
        if credential_wait:
            return _defer_stage(
                batch_id,
                TELEGRAM_SCAN_COMMENTS_STAGE,
                exc,
                token=token,
                countdown=countdown,
            )
        return _retry_stage(
            self, batch_id, TELEGRAM_SCAN_COMMENTS_STAGE, exc, token=token,
            failed_count=pending,
            max_retries=0 if invalid_input else None,
        )


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.TELEGRAM_POPULATE_SOFT_TIME_LIMIT,
    time_limit=settings.TELEGRAM_POPULATE_TIME_LIMIT,
    # Populate deliberately has no automatic retry: expected FloodWait exits
    # are partial, while a lost worker becomes a manually retryable batch.
    max_retries=0,
)
def populate_processing_batch(self, batch_id):
    token = None
    try:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            token = begin_batch_stage_locked(batch, TELEGRAM_POPULATE_STAGE)
            if token is None:
                return {"batch_id": str(batch_id), "status": batch.status}
            auth = _auth_payload(batch)

        populate_seeds.run(
            list(batch.item_ids), auth, token=batch.job.request_token,
            source_batch_id=batch.id, lease_token=token,
        )

        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_POPULATE_STAGE, token=token)
            states = list(SeedItem.objects.filter(pk__in=batch.item_ids).values("is_seeded", "is_valid"))
            seeded = sum(1 for seed in states if seed["is_seeded"])
            invalid = sum(1 for seed in states if not seed["is_valid"])
            deferred = max(batch.item_count - seeded - invalid, 0)
            complete_batch_stage_locked(
                batch,
                stage=TELEGRAM_POPULATE_STAGE,
                token=token,
                processed_count=seeded,
                skipped_count=invalid,
                deferred_count=deferred,
                stage_summary=StageSummary(
                    inputs=[SummaryCount(key="seeds", count=batch.item_count, kind="telegram.seed", unit="seeds")],
                    outputs=[
                        SummaryCount(key="seeds_populated", count=seeded, kind="telegram.seed", unit="seeds")
                    ],
                    skipped=[SummaryReason(key="invalid_seed", count=invalid)] if invalid else [],
                ),
            )
        return {"batch_id": str(batch_id), "status": TelegramTaskOutcome.POPULATED.value}
    except Exception as exc:
        # This stores a terminal failed batch rather than retrying it.
        return _retry_stage(self, batch_id, TELEGRAM_POPULATE_STAGE, exc, token=token, max_retries=0)


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.TELEGRAM_ACCESS_SOFT_TIME_LIMIT,
    time_limit=settings.TELEGRAM_ACCESS_TIME_LIMIT,
    max_retries=settings.TELEGRAM_ACCESS_MAX_RETRIES,
)
def access_rooms_processing_batch(self, batch_id):
    token = None
    try:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            token = begin_batch_stage_locked(batch, TELEGRAM_ACCESS_STAGE)
            if token is None:
                return {"batch_id": str(batch_id), "status": batch.status}
            auth = _auth_payload(batch)
            room_ids = list(
                RoomItem.objects.filter(
                    pk__in=batch.item_ids,
                    is_valid=False,
                    is_deleted=False,
                ).values_list("id", flat=True)
            )

        # A retry after a worker loss only asks Telegram about rooms that have
        # not already been repaired; successful API calls are therefore safe to
        # replay without spending the credential's daily lookup quota twice.
        if room_ids:
            access_entities.run(
                room_ids,
                auth,
                RoomItem.__name__,
                token=batch.job.request_token,
            )

        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_ACCESS_STAGE, token=token)
            states = list(
                RoomItem.objects.filter(pk__in=batch.item_ids).values_list("is_valid", "is_deleted")
            )
            resolved = sum(1 for is_valid, is_deleted in states if is_valid and not is_deleted)
            deleted = sum(1 for _, is_deleted in states if is_deleted)
            deferred = max(batch.item_count - resolved - deleted, 0)
            complete_batch_stage_locked(
                batch,
                stage=TELEGRAM_ACCESS_STAGE,
                token=token,
                processed_count=resolved,
                deferred_count=deferred,
                skipped_count=deleted,
                stage_summary=StageSummary(
                    inputs=[SummaryCount(key="rooms", count=batch.item_count, kind="telegram.room", unit="rooms")],
                    processed=[
                        SummaryCount(key="rooms_resolved", count=resolved, kind="telegram.room", unit="rooms")
                    ],
                    skipped=[SummaryReason(key="room_deleted", count=deleted)] if deleted else [],
                ),
            )
        return {"batch_id": str(batch_id), "status": TelegramTaskOutcome.ACCESS_RECALCULATED.value}
    except Exception as exc:
        return _retry_stage(self, batch_id, TELEGRAM_ACCESS_STAGE, exc, token=token)


# ======================================================
# =====        SEARCH AND FACT SYNCHRONIZATION     =====
# ======================================================


@shared_task(
    bind=True, ignore_result=True, acks_late=True, task_reject_on_worker_lost=True,
    soft_time_limit=settings.INDEX_SOFT_TIME_LIMIT,
    time_limit=settings.INDEX_TIME_LIMIT, max_retries=settings.INDEX_MAX_RETRIES,
)
def sync_message_entity_index_batch(self, batch_id):
    """Apply one durable search/ClickHouse projection or deletion batch.

    This task handles work recorded by Telegram's index deletion signals. NER
    ingestion uses the existing Telegram INDEX stage, which also writes the
    entity facts after NER STORE. PostgreSQL remains the source of truth; a
    failed backend request leaves this batch retryable.
    """
    from app_telegram.services.index import (
        delete_entity_facts, delete_message_ids, index_message_ids,
    )

    token = None
    try:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            token = begin_batch_stage_locked(batch, TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE)
            if token is None:
                return {"batch_id": str(batch_id), "status": batch.status}
            ids = list(batch.item_ids)
            operation = batch.job.filters["operation"]
        # Do network I/O outside the scheduler transaction and row lock.
        if operation == "messages":
            index_message_ids(ids)
        elif operation == "delete_messages":
            delete_message_ids(ids)
        elif operation == "delete_entities":
            delete_entity_facts(ids)
        else:
            raise ValueError("Unknown entity projection operation")
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE, token=token)
            # Mark complete only after both backend operations succeeded.
            complete_batch_stage_locked(
                batch, stage=TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE, token=token, processed_count=len(ids),
                stage_summary=StageSummary(outputs=[
                    SummaryCount(key="items_synchronized", count=len(ids), kind="entity.index", unit="items"),
                ]),
            )
        return {"batch_id": str(batch_id), "status": "indexed"}
    except RoomMetadataWaiting as exc:
        return _defer_stage(batch_id, TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE, exc, token=token, countdown=10)
    except Exception as exc:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            if self.request.retries >= self.max_retries:
                fail_batch_stage_locked(batch, stage=TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE, token=token, error=exc)
                return {"batch_id": str(batch_id), "status": TaskOutcome.FAILED.value}
            countdown = random.randint(20, 60)
            retried = retry_batch_stage_locked(
                batch, stage=TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE, token=token, error=exc, countdown=countdown,
            )
        if retried:
            raise self.retry(exc=exc, countdown=countdown)
        return {"batch_id": str(batch_id), "status": batch.status}


# ======================================================
# =====           TELEGRAM INDEX STAGES            =====
# ======================================================


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.INDEX_SOFT_TIME_LIMIT,
    time_limit=settings.INDEX_TIME_LIMIT,
    max_retries=settings.INDEX_MAX_RETRIES,
)
def index_messages_processing_batch(self, batch_id):
    token = None
    try:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            token = begin_batch_stage_locked(batch, TELEGRAM_INDEX_STAGE)
            if token is None:
                return {"batch_id": str(batch_id), "status": batch.status}
            item_ids = list(batch.item_ids)
            rewrite_messages = batch.job.filters.get("reindex_messages", True)

        # NER backfills always refresh entity facts; rewriting the large search
        # document is optional and follows the caller's explicit reindex choice.
        if rewrite_messages:
            index_counts = index_message_ids(item_ids)
        else:
            index_counts = index_message_ids(item_ids, rewrite_messages=False)

        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_INDEX_STAGE, token=token)
            # A scan handoff can deliberately skip NER for a message already
            # owned/completed elsewhere. Keep that truthful NER accounting when
            # its final index stage closes the generic batch.
            complete_batch_stage_locked(
                batch,
                stage=TELEGRAM_INDEX_STAGE,
                token=token,
                processed_count=(
                    batch.processed_count
                    if (batch.processed_count or batch.failed_count or batch.deferred_count or batch.skipped_count)
                    else batch.item_count
                ),
                failed_count=batch.failed_count,
                deferred_count=batch.deferred_count,
                skipped_count=batch.skipped_count,
                stage_summary=StageSummary(
                    inputs=[
                        SummaryCount(
                            key="messages", count=len(item_ids), kind="telegram.message", unit="messages"
                        )
                    ],
                    outputs=[
                        SummaryCount(
                            key="search_messages_indexed",
                            count=index_counts["search_messages_indexed"],
                            kind="telegram.message",
                            unit="messages",
                        ),
                        SummaryCount(
                            key="analytics_entity_facts_synchronized",
                            count=index_counts["analytics_entity_facts_synchronized"],
                            kind="entity.fact",
                            unit="pairs",
                        ),
                    ],
                    metadata={"search_indexing_enabled": rewrite_messages},
                ),
            )
        return {"batch_id": str(batch_id), "status": TelegramTaskOutcome.INDEXED.value}
    except Exception as exc:
        return _retry_stage(self, batch_id, TELEGRAM_INDEX_STAGE, exc, token=token)


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    soft_time_limit=settings.INDEX_SOFT_TIME_LIMIT,
    time_limit=settings.INDEX_TIME_LIMIT,
    max_retries=settings.INDEX_MAX_RETRIES,
)
def index_rooms_from_seeds_processing_batch(self, batch_id):
    token = None
    try:
        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            token = begin_batch_stage_locked(batch, TELEGRAM_INDEX_ROOMS_STAGE)
            if token is None:
                return {"batch_id": str(batch_id), "status": batch.status}
            room_ids = list(RoomItem.objects.filter(seed_item_id__in=batch.item_ids).values_list("id", flat=True))

        index_room_ids(room_ids)

        with transaction.atomic():
            batch = lock_batch_and_job(batch_id)
            ensure_stage_lease_locked(batch, stage=TELEGRAM_INDEX_ROOMS_STAGE, token=token)
            complete_batch_stage_locked(
                batch,
                stage=TELEGRAM_INDEX_ROOMS_STAGE,
                token=token,
                processed_count=batch.processed_count,
                failed_count=batch.failed_count,
                deferred_count=batch.deferred_count,
                skipped_count=batch.skipped_count,
                stage_summary=StageSummary(
                    inputs=[SummaryCount(key="seeds", count=batch.item_count, kind="telegram.seed", unit="seeds")],
                    outputs=[
                        SummaryCount(
                            key="rooms_submitted_for_indexing",
                            count=len(room_ids),
                            kind="telegram.room",
                            unit="rooms",
                        )
                    ],
                ),
            )
        return {"batch_id": str(batch_id), "status": TelegramTaskOutcome.ROOMS_INDEXED.value}
    except Exception as exc:
        return _retry_stage(self, batch_id, TELEGRAM_INDEX_ROOMS_STAGE, exc, token=token)


@shared_task(
    bind=True,
    track_started=True,
    ignore_result=True,
    acks_late=True,
    task_reject_on_worker_lost=True,
    max_retries=settings.INDEX_MAX_RETRIES,
)
def dispatch_index_backfill_batches(self, job_id):
    try:
        return dispatch_index_backfill(job_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=random.randint(20, 60))


@shared_task(bind=True, track_started=True, ignore_result=True, max_retries=settings.TELEGRAM_SCAN_MAX_RETRIES)
def dispatch_scan_comments_source_batches(self, job_id):
    """Dispatch a comments root's bounded source page from Celery.

    ``job_id`` identifies the durable source. Return new batch IDs; commit cursor
    and batches in the service. On errors raise Celery Retry up to the configured
    cap, then propagate; reconciliation also recovers missed dispatches.
    """
    try:
        return dispatch_scan_comments_source(job_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=random.randint(20, 60))


# These small Celery entry points initialize durable Jobs when Beat triggers a
# schedule. Import them explicitly during Celery autodiscovery so workers
# register their task names without relying on Django view imports.
from app_telegram.services.pipelines import (  # noqa: E402,F401
    access_room_pipeline,
    index_msgs_pipeline,
    populate_pipeline,
    scan_comments_pipeline,
    scan_pipeline,
)
