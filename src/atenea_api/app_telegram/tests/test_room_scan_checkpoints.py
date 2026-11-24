"""Room checkpoints, rollback and retry against isolated PostgreSQL."""

import asyncio
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from asgiref.sync import sync_to_async
from celery.exceptions import Retry
from billiard.exceptions import SoftTimeLimitExceeded
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, close_old_connections, connections
from django.utils import timezone
from telethon.errors import FloodWaitError

from app_base.models import QueryItem
from app_scheduler.models import ProcessingBatch, ProcessingHandoff
from app_scheduler.services.jobs import pause_job, resume_job, reconcile_jobs
from app_scheduler.tests.test_database_concurrency import deadlock
from app_telegram.models import MessageItem, RoomItem
from app_telegram.services.jobs import (
    create_scan_jobs_from_queryset, pending_scan_room_ids, scan_rooms_batch_counts,
)
from app_telegram.services.telegram import _create_scan_message_handoffs
from app_telegram.tasks import scan_rooms_processing_batch
from app_telegram.tests.test_message_collection import (
    FakeAsyncIterator, FakeTelegramClient, telegram_message,
)


pytestmark = pytest.mark.django_db(transaction=True)


class TracedIterator(FakeAsyncIterator):
    def __init__(self, client, room_id, items=(), exception=None):
        super().__init__(items, exception)
        self.client, self.room_id, self.open = client, room_id, True
        client.active += 1
        client.peak = max(client.peak, client.active)

    async def __anext__(self):
        try:
            if self.index == 0 and self.client.hook:
                await self.client.hook(self.room_id)
            await asyncio.sleep(0.001)
            return await super().__anext__()
        except BaseException:
            if self.open:
                self.client.active -= 1
                self.open = False
            raise


class RoomScanClient(FakeTelegramClient):
    responses = {}
    instances = []
    hook = None
    cleanup_error = None
    startup_error = None

    def __init__(self, *args, **kwargs):
        self.calls, self.loops = [], []
        self.active = self.peak = self.opened = self.closed = 0
        type(self).instances.append(self)

    async def __aenter__(self):
        self.opened += 1
        return self

    async def __aexit__(self, *args):
        self.closed += 1
        if self.cleanup_error:
            raise self.cleanup_error
        return False

    async def start(self):
        if self.startup_error:
            raise self.startup_error
        return self

    def iter_messages(self, **kwargs):
        room_id = kwargs["entity"].channel_id
        self.calls.append(room_id)
        self.loops.append(asyncio.get_running_loop())
        messages, error = self.responses.get(room_id, ([telegram_message(msg_id=701)], None))
        return TracedIterator(self, room_id, messages, error)

    def iter_participants(self, **kwargs):
        room_id = kwargs["entity"].channel_id
        self.loops.append(asyncio.get_running_loop())
        user = SimpleNamespace(
            id=room_id + 100000, username=f"user_{room_id}", access_hash=123,
            first_name="Test", last_name="Member", phone=None,
            bot=False, scam=False, participant=None,
        )
        return TracedIterator(self, room_id, [user])


@pytest.fixture(autouse=True)
def client(monkeypatch):
    RoomScanClient.responses, RoomScanClient.instances = {}, []
    RoomScanClient.hook = RoomScanClient.cleanup_error = None
    RoomScanClient.startup_error = None
    monkeypatch.setattr("app_scheduler.services.jobs.current_app.send_task", Mock())
    monkeypatch.setattr("app_telegram.services.telegram.TelegramClient", RoomScanClient)
    monkeypatch.setattr("app_telegram.services.telegram.StringSession", lambda _: "session")
    monkeypatch.setattr("app_telegram.services.telegram.detect_lang", lambda _: "en")
    monkeypatch.setattr("app_telegram.services.telegram.SLEEP_AFTER_X_REQUESTS", 0)
    return RoomScanClient


@pytest.fixture
def make_scan(telegram_auth_factory, room_factory, query, settings):
    def create(count):
        settings.TELEGRAM_SCAN_ROOMS_PER_BATCH = count
        auth = telegram_auth_factory()
        rooms = [
            room_factory(
                tg_id=1000 + index, unique_name=f"checkpoint_{index}",
                link=f"https://t.me/checkpoint_{index}", is_channel=False,
                access_auth=auth, access_hash=123,
            ) for index in range(count)
        ]
        with patch("app_telegram.services.jobs.dispatch_ready_batches"):
            job, _, _ = create_scan_jobs_from_queryset(
                RoomItem.objects.filter(pk__in=[room.pk for room in rooms]),
                filters={}, options={"max_msgs": 100}, request_token=query.pk,
            )
        batch = job.batches.get()
        by_id = {str(room.pk): room for room in rooms}
        return batch, auth, [by_id[room_id] for room_id in batch.item_ids]
    return create


def run_batch(batch, *, retries=0):
    task = scan_rooms_processing_batch
    task.push_request(retries=retries)
    try:
        with patch.object(task, "retry", side_effect=Retry("retry remaining rooms")):
            return task.run(str(batch.pk))
    finally:
        task.pop_request()


def allow_retry(batch, auth):
    past = timezone.now() - timedelta(seconds=1)
    ProcessingBatch.objects.filter(pk=batch.pk).update(run_after=past)
    type(auth).objects.filter(pk=auth.pk).update(wait_until=past)


def assert_stored(batch, rooms, query, expected):
    batch.refresh_from_db()
    assert batch.processed_count == expected
    assert MessageItem.objects.filter(room__in=rooms).count() == expected
    assert sum(ProcessingHandoff.objects.filter(source_batch=batch).values_list("item_count", flat=True)) == expected
    message_type = ContentType.objects.get_for_model(MessageItem, for_concrete_model=False)
    assert QueryItem.objects.filter(query=query, content_type=message_type).count() == expected
    assert RoomItem.objects.filter(pk__in=[room.pk for room in rooms], last_offset=701).count() == expected
    assert sum(room.members.count() for room in rooms) == expected
    assert len(batch.processor_metadata.get("room_progress", {})) == expected


def test_deadlock_at_checkpoint_commit_retries_only_uncommitted_round(make_scan, client, query, monkeypatch):
    batch, _, rooms = make_scan(13)
    connection = connections["default"]
    commit = connection.commit
    failed = False

    def commit_checkpoint():
        nonlocal failed
        progress = ProcessingBatch.objects.values_list("processed_count", flat=True).get(pk=batch.pk)
        if progress == 12 and not failed:
            failed = True
            raise deadlock()
        return commit()

    monkeypatch.setattr(connection, "commit", commit_checkpoint)
    assert run_batch(batch)["status"] == "scanned"
    assert failed
    assert_stored(batch, rooms, query, 13)
    assert batch.stage_attempts[batch.job.workflow[0]] == 1
    calls = Counter(client.instances[0].calls)
    assert all(calls[room.tg_id] == 1 for room in rooms[:6] + rooms[12:])
    assert all(calls[room.tg_id] == 2 for room in rooms[6:12])


def test_callback_deadlock_after_commit_keeps_checkpoint_for_normal_resume(make_scan, client, query):
    batch, auth, rooms = make_scan(13)
    with patch("app_scheduler.services.jobs.refresh_processing_job", side_effect=deadlock()), pytest.raises(Retry):
        run_batch(batch)
    assert_stored(batch, rooms, query, 6)
    assert len(client.instances[0].calls) == 6

    allow_retry(batch, auth)
    assert run_batch(batch)["status"] == "scanned"
    assert_stored(batch, rooms, query, 13)
    assert set(client.instances[1].calls) == {room.tg_id for room in rooms[6:]}


def test_room_99_floodwait_keeps_96_rooms_and_retries_only_the_last_round(make_scan, client, query):
    batch, auth, rooms = make_scan(100)
    original_inputs = list(batch.item_ids)
    client.responses[rooms[98].tg_id] = ([telegram_message(msg_id=701)], FloodWaitError(request=None, capture=240))

    # A credential wait must still defer after generic retries are exhausted.
    assert run_batch(batch, retries=scan_rooms_processing_batch.max_retries)["status"] == "waiting_retry"
    assert_stored(batch, rooms, query, 96)
    auth.refresh_from_db()
    assert batch.status == ProcessingBatch.RETRY_WAIT
    assert batch.run_after >= auth.wait_until + timedelta(seconds=60)
    assert scan_rooms_batch_counts(batch) == (96, 0, 4, 96)
    assert pending_scan_room_ids(batch) == original_inputs[96:]
    first_client = client.instances[0]
    assert first_client.opened == first_client.closed == 1
    assert len(set(first_client.loops)) == 1
    assert first_client.peak == 8
    assert first_client.active == 0

    client.responses.clear()
    allow_retry(batch, auth)
    assert run_batch(batch)["status"] == "scanned"
    assert_stored(batch, rooms, query, 100)
    assert batch.item_ids == original_inputs
    assert batch.status == ProcessingBatch.COMPLETED
    assert batch.failed_count == 0
    assert set(client.instances[1].calls) == {room.tg_id for room in rooms[96:]}
    assert batch.result_summary["stages"][batch.job.workflow[0]]["outputs"][0]["count"] == 100


@pytest.mark.parametrize("failure_at", ["handoff", "progress"])
def test_database_failure_rolls_back_all_active_round_effects(make_scan, query, failure_at):
    batch, auth, rooms = make_scan(13)
    count = 0
    original_save = ProcessingBatch.save

    def handoff(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise IntegrityError("second round handoff failed")
        return _create_scan_message_handoffs(*args, **kwargs)

    def save_progress(instance, *args, **kwargs):
        nonlocal count
        if "processor_metadata" in kwargs.get("update_fields", ()):
            count += 1
            if count == 2:
                raise IntegrityError("second round progress failed")
        return original_save(instance, *args, **kwargs)

    failing_write = (
        patch("app_telegram.services.telegram._create_scan_message_handoffs", side_effect=handoff)
        if failure_at == "handoff" else patch.object(ProcessingBatch, "save", save_progress)
    )
    with failing_write, pytest.raises(Retry):
        run_batch(batch)
    assert_stored(batch, rooms, query, 6)
    assert batch.status == ProcessingBatch.RETRY_WAIT
    assert scan_rooms_batch_counts(batch) == (6, 0, 7, 6)

    allow_retry(batch, auth)
    assert run_batch(batch)["status"] == "scanned"
    assert_stored(batch, rooms, query, 13)


@pytest.mark.parametrize("change", ["pause", "replace_lease"])
def test_pause_or_stale_lease_retains_confirmed_progress(make_scan, client, query, change):
    batch, _, rooms = make_scan(13)
    fired = False

    def mutate_batch():
        close_old_connections()
        try:
            if change == "pause":
                pause_job(batch.job_id)
            else:
                ProcessingBatch.objects.filter(pk=batch.pk).update(lease_token="replacement")
        finally:
            connections.close_all()

    async def hook(_client, room_id):
        nonlocal fired
        # Pause finishes the first active round; lease replacement rejects
        # the second round, after the first one was independently committed.
        target = rooms[0] if change == "pause" else rooms[6]
        if not fired and room_id == target.tg_id:
            fired = True
            await sync_to_async(mutate_batch, thread_sensitive=False)()

    client.hook = hook
    result = run_batch(batch)
    assert result["status"] == ("paused" if change == "pause" else "superseded")
    assert_stored(batch, rooms, query, 6)
    if change == "pause":
        assert batch.status == ProcessingBatch.PENDING
        client.hook = None
        resume_job(batch.job_id)
        assert run_batch(batch)["status"] == "scanned"
        assert_stored(batch, rooms, query, 13)
    else:
        assert batch.lease_token == "replacement"
        assert batch.status == ProcessingBatch.RUNNING


def test_cleanup_failure_after_last_checkpoint_does_not_replay_rooms(make_scan, client, query):
    batch, _, rooms = make_scan(7)
    client.cleanup_error = RuntimeError("client close failed after commit")
    assert run_batch(batch)["status"] == "scanned"
    assert_stored(batch, rooms, query, 7)
    assert batch.status == ProcessingBatch.COMPLETED
    assert len(client.instances) == 1


def test_exhaustion_fails_only_uncommitted_rooms(make_scan, query):
    batch, _, rooms = make_scan(13)
    count = 0

    def handoff(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise IntegrityError("permanent second-round failure")
        return _create_scan_message_handoffs(*args, **kwargs)

    with patch("app_telegram.services.telegram._create_scan_message_handoffs", side_effect=handoff):
        assert run_batch(batch, retries=scan_rooms_processing_batch.max_retries)["status"] == "failed"
    assert_stored(batch, rooms, query, 6)
    assert batch.status == ProcessingBatch.FAILED
    assert batch.failed_count == 7
    assert batch.result_summary["stages"][batch.job.workflow[0]]["failed"] == [{"key": "scan_failed", "count": 7}]


def test_soft_timeout_settles_requests_and_preserves_earlier_rounds(make_scan, client, query):
    batch, _, rooms = make_scan(13)
    client.responses[rooms[7].tg_id] = ([telegram_message(msg_id=701)], SoftTimeLimitExceeded())
    with pytest.raises(Retry):
        run_batch(batch)
    assert_stored(batch, rooms, query, 6)
    assert client.instances[0].active == 0
    assert client.instances[0].closed == 1


def test_startup_floodwait_survives_a_cleanup_error(make_scan, client, query):
    batch, auth, rooms = make_scan(2)
    client.startup_error = FloodWaitError(request=None, capture=240)
    client.cleanup_error = RuntimeError("cleanup must not replace FloodWait")
    assert run_batch(batch)["status"] == "waiting_retry"
    assert_stored(batch, rooms, query, 0)
    auth.refresh_from_db()
    assert batch.run_after >= auth.wait_until + timedelta(seconds=60)


def test_pause_with_floodwait_still_records_the_credential_deadline(make_scan, client, query):
    batch, auth, rooms = make_scan(13)
    fired = False
    client.responses[rooms[6].tg_id] = ([], FloodWaitError(request=None, capture=240))

    def pause():
        close_old_connections()
        try:
            pause_job(batch.job_id)
        finally:
            connections.close_all()

    async def hook(_client, room_id):
        nonlocal fired
        if not fired and room_id == rooms[6].tg_id:
            fired = True
            await sync_to_async(pause, thread_sensitive=False)()

    client.hook = hook
    assert run_batch(batch)["status"] == "waiting_retry"
    assert_stored(batch, rooms, query, 6)
    auth.refresh_from_db()
    assert auth.wait_until > timezone.now()
    assert batch.job.status == batch.job.PAUSED
    assert batch.run_after >= auth.wait_until + timedelta(seconds=60)


@pytest.mark.parametrize("recovery", ["expired_lease", "scheduled_abandonment"])
def test_scheduler_failure_counts_exclude_committed_deferred_rooms(make_scan, client, recovery):
    batch, _, rooms = make_scan(13)
    RoomItem.objects.filter(pk=rooms[0].pk).delete()
    client.responses[rooms[6].tg_id] = ([], FloodWaitError(request=None, capture=240))
    assert run_batch(batch)["status"] == "waiting_retry"
    batch.refresh_from_db()
    assert scan_rooms_batch_counts(batch) == (5, 1, 7, 5)
    if recovery == "expired_lease":
        ProcessingBatch.objects.filter(pk=batch.pk).update(
            recover_on_lease_expiry=False, lease_until=timezone.now() - timedelta(seconds=1),
        )
        reconcile_jobs()
    else:
        from app_scheduler.services.jobs import auto_finalize_scheduled_job
        from app_scheduler.tests.test_schedule_lifecycle import scheduled_policy

        type(batch.job).objects.filter(pk=batch.job_id).update(
            status=batch.job.WAITING_RETRY, lifecycle_policy=scheduled_policy(),
            retry_deadline_at=timezone.now() - timedelta(seconds=1),
        )
        auto_finalize_scheduled_job(batch.job_id)
    batch.refresh_from_db()
    assert batch.status == ProcessingBatch.FAILED
    assert (batch.processed_count, batch.deferred_count, batch.failed_count) == (5, 1, 7)
