from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from app_scheduler.models import ProcessingBatch, ProcessingJob
from app_scheduler.services.jobs import (
    auto_finalize_scheduled_job,
    create_batch_locked,
    create_processing_job,
    refresh_job_locked,
    retry_failed_job_tree,
)
from app_scheduler.schedule_policy import build_scheduler_context, retry_deadline_for


PROCESSOR = "test.scheduled"
WORKFLOW = ["test.stage"]


@pytest.fixture
def publication():
    with patch("app_scheduler.services.jobs.dispatch_ready_batches"), patch(
        "app_scheduler.services.jobs.dispatch_handoffs"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"):
        yield


def scheduled_policy(*, replace_running_job=False, every=4, period="hours"):
    return {
        "scheduled": True,
        "trigger_id": 123,
        "auto_finalize_failed": True,
        "replace_running_job": replace_running_job,
        "retry_grace_seconds": 300,
        "schedule": {"type": "interval", "every": every, "period": period},
    }


def _job(*, fingerprint=None, lifecycle_policy=None):
    job, _ = create_processing_job(
        processor=PROCESSOR,
        mode=ProcessingJob.LIVE,
        workflow=WORKFLOW,
        filters={},
        batch_size=1,
        max_inflight_batches=1,
        fingerprint=fingerprint or uuid4().hex,
        lifecycle_policy=lifecycle_policy,
    )
    return job


@pytest.mark.django_db(transaction=True)
def test_completed_scan_releases_schedule_while_child_waits_for_ner(publication):
    fingerprint = uuid4().hex
    policy = scheduled_policy()
    root = _job(fingerprint=fingerprint, lifecycle_policy=policy)
    child = _job()
    child.parent_job = root
    child.status = ProcessingJob.WAITING_RETRY
    child.failed_batches = 1
    child.save()
    ProcessingBatch.objects.create(
        job=root, ordinal=1, status=ProcessingBatch.COMPLETED,
        item_ids=["room"], item_count=1, completed_stages=WORKFLOW,
    )
    failed = ProcessingBatch.objects.create(
        job=child, ordinal=1, status=ProcessingBatch.FAILED,
        item_ids=["persisted-message"], item_count=1, failed_count=1, current_stage=WORKFLOW[0],
    )
    with transaction.atomic():
        locked = ProcessingJob.objects.select_for_update().get(pk=root.pk)
        locked.input_closed_at = timezone.now()
        locked.save(update_fields=["input_closed_at"])
        refresh_job_locked(locked)

    successor, created = create_processing_job(
        processor=PROCESSOR, mode=ProcessingJob.LIVE, workflow=WORKFLOW, filters={},
        batch_size=1, max_inflight_batches=1, fingerprint=fingerprint, lifecycle_policy=policy,
    )

    root.refresh_from_db()
    child.refresh_from_db()
    failed.refresh_from_db()
    assert created and successor.pk != root.pk
    assert root.status == ProcessingJob.COMPLETED
    assert root.retry_deadline_at is None
    assert child.status == ProcessingJob.WAITING_RETRY
    assert child.failures_abandoned_at is None
    assert child.input_closed_at is not None
    assert failed.status == ProcessingBatch.FAILED


@pytest.mark.django_db(transaction=True)
def test_scan_finalization_preserves_child_failure_and_parent_action_retries_it(publication):
    root = _job(lifecycle_policy=scheduled_policy())
    child = _job()
    now = timezone.now()
    child.parent_job = root
    child.status = ProcessingJob.WAITING_RETRY
    child.input_closed_at = now
    child.failed_batches = 1
    child.save()
    root.status = ProcessingJob.WAITING_RETRY
    root.input_closed_at = now
    root.retry_deadline_at = now - timedelta(seconds=1)
    root.save()
    source = ProcessingBatch.objects.create(
        job=root, ordinal=1, status=ProcessingBatch.FAILED,
        item_ids=["missed-room"], item_count=1, failed_count=1, current_stage=WORKFLOW[0],
    )
    downstream = ProcessingBatch.objects.create(
        job=child, ordinal=1, status=ProcessingBatch.FAILED,
        item_ids=["saved-message"], item_count=1, failed_count=1, current_stage=WORKFLOW[0],
    )

    auto_finalize_scheduled_job(root.pk, now=now)
    root.refresh_from_db()
    child.refresh_from_db()
    assert root.status == ProcessingJob.PARTIAL
    assert root.failures_abandoned_at == now
    assert child.status == ProcessingJob.WAITING_RETRY
    assert child.failures_abandoned_at is None
    assert child.input_outcome == ProcessingJob.INPUT_PARTIAL

    retry_failed_job_tree(root.pk)
    source.refresh_from_db()
    downstream.refresh_from_db()
    root.refresh_from_db()
    assert source.status == ProcessingBatch.FAILED
    assert downstream.status == ProcessingBatch.PENDING
    assert root.status == ProcessingJob.PARTIAL


@pytest.mark.django_db(transaction=True)
def test_scheduled_retry_deadline_is_before_next_interval_with_short_interval_cap():
    now = timezone.now()
    deadline = retry_deadline_for(scheduled_policy(every=1, period="hours"), now)

    assert deadline is not None
    assert timedelta(minutes=44) < deadline - now < timedelta(hours=1)


@pytest.mark.django_db
def test_retry_deadline_uses_last_beat_slot_when_a_running_job_skips_a_tick():
    interval = IntervalSchedule.objects.create(every=4, period=IntervalSchedule.HOURS)
    now = timezone.now()
    trigger = PeriodicTask.objects.create(
        name="anchored-schedule",
        task="app_telegram.services.pipelines.scan_pipeline",
        interval=interval,
        enabled=True,
        last_run_at=now - timedelta(hours=2),
    )

    deadline = retry_deadline_for(build_scheduler_context(trigger), now)

    # Last Beat fire was at 14:00; a failure at 16:00 must still leave the
    # 18:00 slot available, rather than moving the deadline to 19:55.
    assert timedelta(hours=1, minutes=54) < deadline - now < timedelta(hours=1, minutes=56)


@pytest.mark.django_db(transaction=True)
def test_auto_finalize_marks_failed_and_retry_wait_batches_partial():
    job = _job(lifecycle_policy=scheduled_policy())
    now = timezone.now()
    content_type = ContentType.objects.get_for_model(ProcessingJob, for_concrete_model=False)

    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_scheduler.services.jobs.dispatch_ready_batches"
    ), patch("app_scheduler.services.jobs.dispatch_handoffs"), patch(
        "app_scheduler.services.jobs.enqueue_source_dispatch"
    ):
        with transaction.atomic():
            locked = ProcessingJob.objects.select_for_update().get(pk=job.pk)
            failed = create_batch_locked(
                locked,
                item_ids=["failed"],
                item_content_type=content_type,
                schedule_dispatch=False,
            )
            retry_wait = create_batch_locked(
                locked,
                item_ids=["retry-wait"],
                item_content_type=content_type,
                schedule_dispatch=False,
            )
            for batch, status in (
                (failed, ProcessingBatch.FAILED),
                (retry_wait, ProcessingBatch.RETRY_WAIT),
            ):
                batch.status = status
                batch.failed_count = batch.item_count if status == ProcessingBatch.FAILED else 0
                batch.run_after = now + timedelta(hours=1) if status == ProcessingBatch.RETRY_WAIT else None
                batch.completed_at = now if status == ProcessingBatch.FAILED else None
                batch.save(
                    update_fields=[
                        "status", "failed_count", "run_after", "completed_at", "updated_at",
                    ]
                )
            locked.input_closed_at = now
            locked.status = ProcessingJob.WAITING_RETRY
            locked.retry_deadline_at = now - timedelta(seconds=1)
            locked.save(
                update_fields=[
                    "input_closed_at", "status", "retry_deadline_at", "updated_at",
                ]
            )

        finalized = auto_finalize_scheduled_job(job.pk, now=now)

    finalized.refresh_from_db()
    failed.refresh_from_db()
    retry_wait.refresh_from_db()
    assert finalized.status == ProcessingJob.PARTIAL
    assert finalized.input_outcome == ProcessingJob.INPUT_PARTIAL
    assert finalized.failures_abandoned_at is not None
    assert finalized.retry_deadline_at is None
    assert failed.status == ProcessingBatch.FAILED
    assert retry_wait.status == ProcessingBatch.FAILED
    assert "schedule policy" in retry_wait.last_error


@pytest.mark.django_db(transaction=True)
def test_scheduled_tick_finalizes_due_job_and_creates_successor():
    policy = scheduled_policy()
    fingerprint = uuid4().hex
    old = _job(fingerprint=fingerprint, lifecycle_policy=policy)
    ProcessingBatch.objects.create(
        job=old,
        ordinal=1,
        status=ProcessingBatch.FAILED,
        item_ids=["failed"],
        item_count=1,
        failed_count=1,
        current_stage=WORKFLOW[0],
        completed_at=timezone.now(),
    )
    old.status = ProcessingJob.WAITING_RETRY
    old.input_closed_at = timezone.now()
    old.retry_deadline_at = timezone.now() - timedelta(seconds=1)
    old.save(update_fields=["status", "input_closed_at", "retry_deadline_at", "updated_at"])

    with patch("app_scheduler.services.jobs.dispatch_ready_batches"), patch(
        "app_scheduler.services.jobs.dispatch_handoffs"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"):
        successor, created = create_processing_job(
            processor=PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=WORKFLOW,
            filters={},
            batch_size=1,
            max_inflight_batches=1,
            fingerprint=fingerprint,
            lifecycle_policy=policy,
        )

    old.refresh_from_db()
    assert created is True
    assert successor.pk != old.pk
    assert old.status == ProcessingJob.PARTIAL


@pytest.mark.django_db
def test_running_scheduled_job_is_reused_by_default_and_replace_is_explicit():
    fingerprint = uuid4().hex
    first = _job(fingerprint=fingerprint, lifecycle_policy=scheduled_policy())
    reused, reused_created = create_processing_job(
        processor=PROCESSOR,
        mode=ProcessingJob.LIVE,
        workflow=WORKFLOW,
        filters={},
        batch_size=1,
        max_inflight_batches=1,
        fingerprint=fingerprint,
        lifecycle_policy=scheduled_policy(),
    )
    replaced, replaced_created = create_processing_job(
        processor=PROCESSOR,
        mode=ProcessingJob.LIVE,
        workflow=WORKFLOW,
        filters={},
        batch_size=1,
        max_inflight_batches=1,
        fingerprint=fingerprint,
        lifecycle_policy=scheduled_policy(replace_running_job=True),
    )

    first.refresh_from_db()
    assert reused.pk == first.pk
    assert reused_created is False
    assert replaced_created is True
    assert replaced.pk != first.pk
    assert first.status == ProcessingJob.CANCELLED
