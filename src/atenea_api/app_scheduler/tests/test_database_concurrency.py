"""Real PostgreSQL lock ordering, foreign-key checks and transaction retries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Barrier
import time
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError, connections, transaction
from django.utils import timezone

from app_scheduler.db import DEADLOCK_ATTEMPTS, lock_batch_and_job, retry_on_deadlock
from app_scheduler.models import ProcessingBatch, ProcessingHandoff, ProcessingJob
from app_scheduler.services.jobs import (
    dispatch_handoffs, dispatch_ready_batches, reconcile_jobs, retry_failed_jobs,
)


pytestmark = pytest.mark.django_db(transaction=True)
STAGE = "test.database.stage"


@pytest.fixture(autouse=True)
def publication(monkeypatch, settings):
    settings.PROCESSING_STAGE_TASKS = {
        **settings.PROCESSING_STAGE_TASKS,
        STAGE: {"task": "test.database.task", "queue": "default", "lease_seconds": 60},
    }
    publish = Mock()
    monkeypatch.setattr("app_scheduler.services.jobs.enqueue_batch_stage", publish)
    monkeypatch.setattr("app_scheduler.services.jobs.enqueue_handoff_dispatch", Mock())
    monkeypatch.setattr("app_scheduler.services.jobs.enqueue_source_dispatch", Mock())
    return publish


def job(**overrides):
    values = {
        "processor": "test.database", "mode": ProcessingJob.BACKFILL,
        "workflow": [STAGE], "fingerprint": uuid4().hex,
        "status": ProcessingJob.RUNNING, "batch_size": 1,
        "max_inflight_batches": 1, "input_closed_at": timezone.now(),
    }
    values.update(overrides)
    return ProcessingJob.objects.create(**values)


def batch(owner, ordinal=1, **overrides):
    values = {
        "job": owner, "ordinal": ordinal, "item_ids": [str(uuid4())],
        "item_count": 1, "current_stage": STAGE,
    }
    values.update(overrides)
    return ProcessingBatch.objects.create(**values)


def separate_connection(operation):
    connection = connections["default"]
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '4s'")
        return operation()
    finally:
        connection.close()


def deadlock():
    class PostgreSQLDeadlock(Exception):
        pgcode = "40P01"

    error = OperationalError("injected deadlock")
    error.__cause__ = PostgreSQLDeadlock()
    return error


def test_foreign_keys_can_commit_while_batch_and_job_are_locked():
    owner = job()
    source = batch(owner)
    content_type = ContentType.objects.get_for_model(ProcessingJob)

    def create_handoff():
        with transaction.atomic():
            return ProcessingHandoff.objects.create(
                source_batch_id=source.pk, target_job_id=owner.pk,
                item_content_type=content_type, item_ids=source.item_ids, item_count=1,
            ).pk

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            lock_batch_and_job(source.pk)
            handoff_id = executor.submit(separate_connection, create_handoff).result(timeout=6)
    assert ProcessingHandoff.objects.filter(pk=handoff_id).exists()


def test_non_key_job_lock_still_excludes_other_writers():
    owner = job()
    source = batch(owner)

    def other_writer():
        try:
            with transaction.atomic():
                ProcessingJob.objects.select_for_update(no_key=True, nowait=True).get(pk=owner.pk)
        except OperationalError as error:
            return error.__cause__.pgcode
        return None

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            lock_batch_and_job(source.pk)
            assert executor.submit(separate_connection, other_writer).result(timeout=6) == "55P03"


def test_waiting_for_job_does_not_lock_its_batch_first():
    owner = job()
    source = batch(owner)
    backend = Queue()

    def waiter():
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend.put(cursor.fetchone()[0])
        with transaction.atomic():
            return lock_batch_and_job(source.pk).pk

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            ProcessingJob.objects.select_for_update(no_key=True).get(pk=owner.pk)
            future = executor.submit(separate_connection, waiter)
            pid = backend.get(timeout=4)
            deadline = time.monotonic() + 3
            while True:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT cardinality(pg_blocking_pids(%s))", [pid])
                    if cursor.fetchone()[0]:
                        break
                assert time.monotonic() < deadline, "Worker did not reach the Job lock"
                time.sleep(0.01)
            ProcessingBatch.objects.select_for_update(nowait=True).get(pk=source.pk)
        assert future.result(timeout=6) == source.pk


def test_bulk_retry_locks_intermediate_jobs_before_requested_descendants():
    root = job(status=ProcessingJob.WAITING_RETRY, failed_batches=1)
    middle = job(parent_job=root)
    leaf = job(parent_job=middle, status=ProcessingJob.WAITING_RETRY, failed_batches=1)
    batch(root, status=ProcessingBatch.FAILED)
    batch(leaf, status=ProcessingBatch.FAILED)
    backend = Queue()

    def retry_tree():
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend.put(cursor.fetchone()[0])
        return retry_failed_jobs([root.pk, leaf.pk])

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            ProcessingJob.objects.select_for_update(no_key=True).get(pk=middle.pk)
            future = executor.submit(separate_connection, retry_tree)
            pid = backend.get(timeout=4)
            deadline = time.monotonic() + 3
            while True:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT cardinality(pg_blocking_pids(%s))", [pid])
                    if cursor.fetchone()[0]:
                        break
                assert time.monotonic() < deadline, "Worker did not reach the intermediate Job lock"
                time.sleep(0.01)
            ProcessingJob.objects.select_for_update(no_key=True, nowait=True).get(pk=leaf.pk)
        assert future.result(timeout=6) == {"jobs": 2, "batches": 2}


def test_bulk_retry_can_preserve_manually_paused_jobs():
    owner = job(status=ProcessingJob.PAUSED, failed_batches=1)
    source = batch(owner, status=ProcessingBatch.FAILED)
    assert retry_failed_jobs([owner.pk], include_paused=False) == {"jobs": 0, "batches": 0}
    owner.refresh_from_db()
    source.refresh_from_db()
    assert owner.status == ProcessingJob.PAUSED
    assert source.status == ProcessingBatch.FAILED


def test_handoff_dispatch_does_not_lock_shared_content_type():
    source_job = job(status=ProcessingJob.COMPLETED)
    source = batch(source_job, status=ProcessingBatch.COMPLETED)
    target = job()
    content_type = ContentType.objects.get_for_model(ProcessingJob)
    handoff = ProcessingHandoff.objects.create(
        source_batch=source, target_job=target, item_content_type=content_type,
        item_ids=source.item_ids, item_count=1,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            ContentType.objects.select_for_update(no_key=True).get(pk=content_type.pk)
            result = executor.submit(separate_connection, lambda: dispatch_handoffs(target.pk)).result(timeout=6)
    handoff.refresh_from_db()
    assert len(result) == 1
    assert handoff.status == ProcessingHandoff.MATERIALIZED


def test_dispatch_only_locks_candidates_needed_for_available_capacity():
    owner = job()
    first, second = batch(owner), batch(owner, 2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            ProcessingBatch.objects.select_for_update().get(pk=second.pk)
            result = executor.submit(separate_connection, lambda: dispatch_ready_batches(owner.pk)).result(timeout=6)
    assert result == [str(first.pk)]
    second.refresh_from_db()
    assert second.queued_at is None


def test_zero_capacity_queues_no_extra_batch(publication):
    owner = job()
    batch(owner, status=ProcessingBatch.RUNNING)
    pending = batch(owner, 2)
    assert dispatch_ready_batches(owner.pk) == []
    pending.refresh_from_db()
    assert pending.queued_at is None
    publication.assert_not_called()


def test_dispatch_fills_slots_without_running_same_resource_twice():
    owner = job(max_inflight_batches=2)
    first = batch(owner, resource_key="auth-a")
    duplicate = batch(owner, 2, resource_key="auth-a")
    third = batch(owner, 3, resource_key="auth-b")
    assert dispatch_ready_batches(owner.pk) == [str(first.pk), str(third.pk)]
    duplicate.refresh_from_db()
    assert duplicate.queued_at is None


def test_due_retry_respects_capacity_of_other_running_work():
    owner = job()
    batch(owner, status=ProcessingBatch.RUNNING, lease_until=timezone.now() + timedelta(minutes=5))
    due = batch(
        owner, 2, status=ProcessingBatch.RETRY_WAIT,
        run_after=timezone.now() - timedelta(seconds=1),
        lease_until=timezone.now() + timedelta(minutes=5),
    )
    assert reconcile_jobs()["recovered_batches"] == 1
    due.refresh_from_db()
    assert due.status == ProcessingBatch.PENDING
    assert due.queued_at is None


def test_real_deadlock_retries_whole_transaction_once_without_duplicate_work():
    owners = [job(), job()]
    rendezvous = Barrier(2)
    committed = []

    def worker(index):
        attempts = 0

        @retry_on_deadlock
        def write():
            nonlocal attempts
            attempts += 1
            ProcessingJob.objects.select_for_update(no_key=True).get(pk=owners[index].pk)
            batch(owners[index])
            transaction.on_commit(lambda: committed.append(index))
            if attempts == 1:
                rendezvous.wait(timeout=4)
            ProcessingJob.objects.select_for_update(no_key=True).get(pk=owners[1 - index].pk)

        write()
        return attempts

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(separate_connection, lambda index=index: worker(index)) for index in range(2)]
        attempts = [future.result(timeout=12) for future in futures]
    assert sorted(attempts) == [1, 2]
    assert sorted(committed) == [0, 1]
    assert ProcessingBatch.objects.filter(job__in=owners).count() == 2


def test_deadlock_during_commit_rolls_back_before_retry(monkeypatch):
    owner = job()
    connection = connections["default"]
    commit = connection.commit
    attempts, committed = [], []

    def fail_first_commit():
        if len(attempts) == 1:
            raise deadlock()
        return commit()

    monkeypatch.setattr(connection, "commit", fail_first_commit)

    @retry_on_deadlock
    def write():
        attempts.append(1)
        batch(owner)
        transaction.on_commit(lambda: committed.append(1))

    write()
    assert len(attempts) == 2
    assert committed == [1]
    assert owner.batches.count() == 1


def test_deadlock_in_callback_does_not_replay_committed_operation():
    owner = job()
    attempts = []

    def failing_callback():
        raise deadlock()

    @retry_on_deadlock
    def write():
        attempts.append(1)
        batch(owner)
        transaction.on_commit(failing_callback)

    with pytest.raises(OperationalError):
        write()
    assert attempts == [1]
    assert owner.batches.count() == 1


def test_deadlock_retries_are_bounded_and_rollback_callbacks():
    owner = job()
    attempts, committed = [], []

    @retry_on_deadlock
    def write():
        attempts.append(1)
        batch(owner)
        transaction.on_commit(lambda: committed.append(1))
        raise deadlock()

    with pytest.raises(OperationalError):
        write()
    assert len(attempts) == DEADLOCK_ATTEMPTS
    assert owner.batches.count() == 0
    assert committed == []


def test_nested_transaction_leaves_retry_to_outer_owner():
    attempts = []

    @retry_on_deadlock
    def write():
        attempts.append(1)
        raise deadlock()

    with pytest.raises(OperationalError), transaction.atomic():
        write()
    assert attempts == [1]


def test_other_database_errors_are_not_retried():
    operation = Mock(side_effect=OperationalError("connection unavailable"))
    with pytest.raises(OperationalError):
        retry_on_deadlock(operation)()
    operation.assert_called_once()
