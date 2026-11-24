"""Comments cursor, parent transactions and recovery against real PostgreSQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from asgiref.sync import sync_to_async
from celery.exceptions import Retry
from billiard.exceptions import SoftTimeLimitExceeded
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone
from telethon.errors import FloodWaitError, MsgIdInvalidError, MsgTooOldError, RPCError

from app_scheduler.models import ProcessingBatch, ProcessingHandoff, ProcessingJob
from app_scheduler.services.jobs import (
    begin_batch_stage_locked, cancel_job, pause_job, resume_job, retry_failed_job,
    reconcile_jobs,
)
from app_telegram.models import MessageItem, RoomItem
from app_telegram.services.jobs import (
    COMMENTS_MESSAGES_INPUT_VERSION, TELEGRAM_SCAN_COMMENTS_STAGE,
    create_scan_comments_jobs, dispatch_scan_comments_source,
)
from app_telegram.services.telegram import (
    ExtractMessagesResult, _extract_messages_result, _scan_comment_parent,
)
from app_telegram.tasks import scan_comments_processing_batch
from app_telegram.tests.test_message_collection import (
    FakeAsyncIterator, FakeTelegramClient, telegram_message,
)


pytestmark = pytest.mark.django_db(transaction=True)


class CommentsClient(FakeTelegramClient):
    """Script responses per parent and record Telethon's exact request inputs."""

    responses = {}
    calls = []
    loops = []

    def iter_messages(self, **kwargs):
        type(self).calls.append(kwargs)
        type(self).loops.append(asyncio.get_running_loop())
        messages, error = self.responses.get(kwargs["reply_to"], ([], None))
        messages = [message for message in messages if message.id > kwargs["offset_id"]]
        return FakeAsyncIterator(messages[:kwargs["limit"]], exception=error)


@pytest.fixture(autouse=True)
def fake_external_services(monkeypatch):
    monkeypatch.setattr("app_scheduler.services.jobs.current_app.send_task", Mock())


@pytest.fixture
def client(monkeypatch):
    CommentsClient.responses = {}
    CommentsClient.calls = []
    CommentsClient.loops = []
    monkeypatch.setattr("app_telegram.services.telegram.TelegramClient", CommentsClient)
    monkeypatch.setattr("app_telegram.services.telegram.StringSession", lambda _: "session")
    monkeypatch.setattr("app_telegram.services.telegram.detect_lang", lambda _: "en")
    # Lock deadlines should measure coordination, not the preventive ten-second
    # pacing applied to real Telegram requests.
    monkeypatch.setattr("app_telegram.services.telegram.SLEEP_AFTER_X_REQUESTS", 0)
    monkeypatch.setattr("app_telegram.services.telegram.SLEEP_AFTER_EMPTY_COMMENT_PARENT", 0)
    return CommentsClient


@pytest.fixture
def parents(room_factory, telegram_auth_factory, message_factory):
    auth = telegram_auth_factory()
    room = room_factory(access_auth=auth, access_hash=123)
    items = [
        message_factory(room=room, msg_id=index, link=f"{room.link}/{index}", is_reply=False)
        for index in range(1, 4)
    ]
    return auth, room, items


def make_job(rooms, *, options=None, filters=None):
    return create_scan_comments_jobs(
        room_ids=[str(room.pk) for room in rooms], filters=filters or {},
        options=options or {"max_msgs": 100},
    )[:2]


def make_batch(room, **kwargs):
    job, child = make_job([room], **kwargs)
    dispatch_scan_comments_source(job.id)
    return job.batches.get(), child


def lease(batch):
    with transaction.atomic():
        locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.pk)
        return begin_batch_stage_locked(locked, TELEGRAM_SCAN_COMMENTS_STAGE)


def run_batch(batch, *, retries=0):
    task = scan_comments_processing_batch
    task.push_request(retries=retries)
    try:
        with patch.object(task, "retry", side_effect=Retry("retry pending parents")):
            return task.run(str(batch.id))
    finally:
        task.pop_request()


def reply(parent, msg_id):
    return telegram_message(
        msg_id=msg_id, text=f"Reply {msg_id}", is_reply=True,
        reply_to=SimpleNamespace(reply_to_msg_id=parent.msg_id),
    )


def allow_retry(batch):
    ProcessingBatch.objects.filter(pk=batch.id).update(run_after=timezone.now() - timedelta(seconds=1))


def test_source_pages_are_bounded_grouped_and_idempotent(settings, parents, telegram_auth_factory, room_factory, message_factory):
    settings.TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH = 2
    settings.TELEGRAM_SCAN_MAX_INFLIGHT = 2
    _, first_room, items = parents
    auth = telegram_auth_factory(name="second", api_id=54321, api_hash="second")
    second_room = room_factory(tg_id=222, link="https://t.me/second", access_auth=auth)
    for index in range(4, 8):
        items.append(message_factory(room=second_room, msg_id=index, link=f"{second_room.link}/{index}"))
    job, child = make_job([first_room, second_room])
    assert job.mode == ProcessingJob.BACKFILL
    assert child.mode == ProcessingJob.LIVE
    assert job.total_items == 0
    assert job.filters["selected_items_count"] == 7

    seen = []
    # Suppress publication to prove even unpublished input prevents a new page.
    with patch("app_telegram.services.jobs.dispatch_ready_batches"):
        while not job.source_exhausted:
            batch_ids = dispatch_scan_comments_source(job.pk)
            job.refresh_from_db()
            if not batch_ids:
                continue
            batches = list(job.batches.filter(pk__in=batch_ids))
            assert sum(batch.item_count for batch in batches) <= 4
            assert dispatch_scan_comments_source(job.pk) == []
            for batch in batches:
                assert batch.item_count <= 2
                assert batch.processor_version == COMMENTS_MESSAGES_INPUT_VERSION
                assert batch.item_content_type == ContentType.objects.get_for_model(MessageItem)
                auth_ids = set(MessageItem.objects.filter(pk__in=batch.item_ids).values_list("room__access_auth_id", flat=True))
                assert {str(auth_id) for auth_id in auth_ids} == {batch.processor_metadata["auth_id"]}
                assert batch.resource_key == f"telegram-auth:{next(iter(auth_ids))}"
                seen.extend(batch.item_ids)
            ProcessingBatch.objects.filter(pk__in=batch_ids).update(status=ProcessingBatch.COMPLETED)
    assert set(seen) == {str(item.id) for item in items}
    assert len(seen) == len(set(seen)) == 7
    assert job.input_closed_at is not None
    assert job.total_items == 7


def test_selection_uses_publication_ingestion_snapshot_and_null_phase(parents, message_factory):
    _, room, items = parents
    now = timezone.now()
    MessageItem.objects.filter(pk__in=[item.pk for item in items]).update(created_at=now - timedelta(days=10))
    MessageItem.objects.filter(pk=items[0].pk).update(stored_date=now - timedelta(days=1))
    MessageItem.objects.filter(pk=items[1].pk).update(stored_date=None)
    MessageItem.objects.filter(pk=items[2].pk).update(is_reply=True)
    old = message_factory(room=room, msg_id=9, link=f"{room.link}/9", created_at=now - timedelta(days=30))
    job, _ = make_job([room], options={"max_msgs": 10, "message_filters": {"and_filter_fields": {
        "created_at__gte": now - timedelta(days=15), "created_at__lte": now,
    }}})
    late = message_factory(room=room, msg_id=10, link=f"{room.link}/10", created_at=now - timedelta(days=10))
    assert late.stored_date > job.source_snapshot_at
    assert job.filters["selected_items_count"] == 2
    seen = []
    with patch("app_telegram.services.jobs.dispatch_ready_batches"):
        for _ in range(5):
            batch_ids = dispatch_scan_comments_source(job.pk)
            seen.extend(parent_id for batch in job.batches.filter(pk__in=batch_ids) for parent_id in batch.item_ids)
            ProcessingBatch.objects.filter(pk__in=batch_ids).update(status=ProcessingBatch.COMPLETED)
    assert set(seen) == {str(items[0].id), str(items[1].id)}
    assert str(old.pk) not in seen
    assert str(late.pk) not in seen
    # An explicit ingestion lower bound excludes NULL (unknown date) parents.
    newer, _ = make_job([room], options={"max_msgs": 10, "message_filters": {"and_filter_fields": {
        "stored_date__gte": now,
    }}})
    assert newer.filters["selected_items_count"] == 2  # old publication + late import


def test_cursor_and_batches_roll_back_together(settings, parents):
    settings.TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH = 1
    settings.TELEGRAM_SCAN_MAX_INFLIGHT = 3
    _, room, _ = parents
    job, _ = make_job([room])
    from app_telegram.services.jobs import create_batch_locked
    calls = []

    def fail_after_first(*args, **kwargs):
        if calls:
            raise IntegrityError("simulate failure while registering the page")
        calls.append(True)
        return create_batch_locked(*args, **kwargs)

    with patch("app_telegram.services.jobs.create_batch_locked", side_effect=fail_after_first), pytest.raises(IntegrityError):
        dispatch_scan_comments_source(job.id)
    job.refresh_from_db()
    assert job.source_cursor == {}
    assert not job.batches.exists()


@pytest.mark.parametrize("invalid_error", [MsgIdInvalidError, MsgTooOldError])
def test_empty_parent_remains_valid_and_telegram_invalid_parent_is_terminal(parents, client, invalid_error):
    _, room, items = parents
    client.responses[items[1].msg_id] = ([], invalid_error(request=None))
    batch, _ = make_batch(room)
    assert run_batch(batch)["status"] == "comments_scanned"
    items[0].refresh_from_db()
    items[1].refresh_from_db()
    assert items[0].is_valid and items[0].last_scan_at is not None
    assert not items[1].is_valid
    batch.refresh_from_db()
    assert batch.processed_count == 2
    assert batch.skipped_count == 1
    assert batch.failed_count == 0
    assert batch.processor_metadata["parent_progress"][str(items[1].id)]["reason"] == invalid_error.__name__
    dispatch_scan_comments_source(batch.job_id)  # finish the dated phase
    dispatch_scan_comments_source(batch.job_id)  # close the empty NULL phase
    next_job, _ = make_job([room])
    assert next_job.id != batch.job_id
    assert next_job.filters["selected_items_count"] == 2


def test_partial_retry_keeps_offsets_handoffs_and_cumulative_limit(parents, client):
    _, room, items = parents
    parent = items[0]
    client.responses[parent.msg_id] = ([reply(parent, 201)], RPCError(None, "TEMPORARY", 500))
    batch, _ = make_batch(room, options={"max_msgs": 3})
    original_ids = list(batch.item_ids)
    with pytest.raises(Retry):
        run_batch(batch)
    parent.refresh_from_db()
    assert parent.last_offset == 201
    batch.refresh_from_db()
    assert batch.processor_metadata["parent_progress"][str(parent.id)]["fetched_count"] == 1
    assert batch.processed_count == 2
    assert ProcessingHandoff.objects.filter(source_batch=batch).count() == 1
    client.responses[parent.msg_id] = ([reply(parent, index) for index in (201, 202, 203, 204)], None)
    allow_retry(batch)
    assert run_batch(batch, retries=1)["status"] == "comments_scanned"
    batch.refresh_from_db()
    assert batch.item_ids == original_ids
    assert batch.processed_count == 3
    assert batch.processor_metadata["parent_progress"][str(parent.id)]["fetched_count"] == 3
    assert [call["reply_to"] for call in client.calls].count(parent.msg_id) == 2
    assert client.calls[-1]["limit"] == 2
    assert client.calls[-1]["offset_id"] == 201
    assert len(set(client.loops)) == 2  # one client loop per attempt
    assert MessageItem.objects.filter(reply_to_msg=parent).count() == 3
    assert sum(ProcessingHandoff.objects.filter(source_batch=batch).values_list("item_count", flat=True)) == 3


def test_worker_failure_preserves_completed_parents_and_fails_only_pending(parents, client):
    _, room, items = parents
    client.responses[items[0].msg_id] = ([reply(items[0], 301)], None)
    client.responses[items[1].msg_id] = ([], MsgIdInvalidError(None))
    client.responses[items[2].msg_id] = ([], RuntimeError("worker operation interrupted"))
    batch, _ = make_batch(room)
    assert run_batch(batch, retries=scan_comments_processing_batch.max_retries)["status"] == "failed"
    batch.refresh_from_db()
    batch.job.refresh_from_db()
    assert (batch.processed_count, batch.skipped_count, batch.failed_count) == (1, 1, 1)
    assert batch.job.processed_items == 1 and batch.job.failed_items == 1
    assert ProcessingHandoff.objects.filter(source_batch=batch).count() == 1
    retry_failed_job(batch.job_id)
    client.responses[items[2].msg_id] = ([], None)
    assert run_batch(batch)["status"] == "comments_scanned"
    assert [call["reply_to"] for call in client.calls] == [1, 2, 3, 3]
    assert ProcessingHandoff.objects.filter(source_batch=batch).count() == 1


def test_conversion_failure_does_not_advance_past_unstored_comment(parents, client):
    _, room, items = parents
    client.responses[1] = ([reply(items[0], index) for index in (201, 202, 203)], None)
    batch, _ = make_batch(room, options={"max_msgs": 3})
    with patch("app_telegram.services.telegram.detect_lang", side_effect=["en", ValueError("conversion failed")]), pytest.raises(Retry):
        run_batch(batch)
    items[0].refresh_from_db()
    batch.refresh_from_db()
    assert items[0].last_offset == 201
    assert batch.processor_metadata["parent_progress"][str(items[0].pk)]["fetched_count"] == 1
    assert MessageItem.objects.filter(reply_to_msg=items[0]).count() == 1
    allow_retry(batch)
    assert run_batch(batch, retries=1)["status"] == "comments_scanned"
    assert MessageItem.objects.filter(reply_to_msg=items[0]).count() == 3


def test_handoff_failure_rolls_back_comments_offset_and_progress(parents, client):
    _, room, items = parents
    client.responses[items[0].msg_id] = ([reply(items[0], 201)], None)
    batch, _ = make_batch(room)
    with patch("app_telegram.services.telegram._create_scan_message_handoffs", side_effect=IntegrityError("handoff failed")):
        with pytest.raises(Retry):
            run_batch(batch)
    items[0].refresh_from_db()
    batch.refresh_from_db()
    assert items[0].last_offset == 0
    assert items[0].last_scan_at is None
    assert not MessageItem.objects.filter(is_reply=True).exists()
    assert not ProcessingHandoff.objects.filter(source_batch=batch).exists()
    assert str(items[0].id) not in batch.processor_metadata["parent_progress"]


def test_floodwait_commits_partial_progress_and_defers_without_sleep(parents, client):
    auth, room, items = parents
    client.responses[1] = ([reply(items[0], 201)], FloodWaitError(None, capture=120))
    batch, _ = make_batch(room)
    before = timezone.now()
    result = run_batch(batch, retries=scan_comments_processing_batch.max_retries)
    auth.refresh_from_db()
    batch.refresh_from_db()
    items[0].refresh_from_db()
    assert result["status"] == "waiting_retry"
    assert batch.status == ProcessingBatch.RETRY_WAIT
    assert batch.failed_count == 0
    assert auth.wait_until >= before + timedelta(seconds=120)
    assert batch.run_after >= auth.wait_until + timedelta(seconds=59)
    assert items[0].last_offset == 201
    assert [call["reply_to"] for call in client.calls] == [1]
    assert batch.processor_metadata["parent_progress"][str(items[0].pk)]["state"] == "pending"


def test_postgresql_lock_skips_one_parent_and_processes_others(parents, client):
    auth, room, items = parents
    first_batch, _ = make_batch(room)
    second_batch, _ = make_batch(room, filters={"separate_run": True})
    first_token = lease(first_batch)
    entered, release = Event(), Event()

    def blocking_extract(*, related_msg, **kwargs):
        entered.set()
        assert release.wait(10)
        related_msg.last_offset = 201
        return ExtractMessagesResult(parent=related_msg)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(_scan_comment_parent, first_batch.id, first_token, str(items[0].id), blocking_extract,
                            auth_id=auth.id, max_msgs=10)
        try:
            assert entered.wait(10)
            assert run_batch(second_batch)["status"] == "comments_scanned"
            second_batch.refresh_from_db()
            assert second_batch.status == ProcessingBatch.COMPLETED
            assert second_batch.processed_count == 2
            assert second_batch.skipped_count == 1
            assert second_batch.failed_count == 0
            skipped = second_batch.processor_metadata["parent_progress"][str(items[0].id)]
            assert skipped["state"] == "skipped" and skipped["reason"] == "parent_locked"
            assert {call["reply_to"] for call in client.calls} == {2, 3}
        finally:
            release.set()
        assert owner.result(10)["state"] == "completed"
    # Redelivery does not retry a parent skipped due to contention.
    calls_before_redelivery = list(client.calls)
    assert run_batch(second_batch)["status"] == ProcessingBatch.COMPLETED
    assert client.calls == calls_before_redelivery
    items[0].refresh_from_db()
    assert items[0].is_valid

    # A future execution still selects the parent and reads the committed offset.
    third_batch, _ = make_batch(room, filters={"future_run": True})
    third_token = lease(third_batch)
    observed = []

    def resumed_extract(*, related_msg, **kwargs):
        observed.append(related_msg.last_offset)
        return ExtractMessagesResult(parent=related_msg)

    assert _scan_comment_parent(third_batch.id, third_token, str(items[0].id), resumed_extract,
                                auth_id=auth.id, max_msgs=10)["state"] == "completed"
    assert observed == [201]


@pytest.mark.parametrize("action", ["cancel", "replace_lease"])
def test_cancel_or_replaced_lease_rejects_parent_commit(parents, client, action):
    _, room, items = parents
    batch, _ = make_batch(room)
    client.responses[1] = ([reply(items[0], 201)], None)

    async def changed_lease(**kwargs):
        result = await _extract_messages_result(**kwargs)
        if action == "cancel":
            await sync_to_async(cancel_job, thread_sensitive=False)(batch.job_id)
        else:
            await sync_to_async(lambda: ProcessingBatch.objects.filter(pk=batch.id).update(lease_token="replacement"), thread_sensitive=False)()
        return result

    with patch("app_telegram.services.telegram._extract_messages_result", side_effect=changed_lease):
        assert run_batch(batch)["status"] == "superseded"
    items[0].refresh_from_db()
    assert items[0].last_offset == 0
    assert not MessageItem.objects.filter(is_reply=True).exists()
    assert not ProcessingHandoff.objects.filter(source_batch=batch).exists()


def test_pause_commits_current_parent_and_resume_processes_pending(parents, client):
    _, room, items = parents
    batch, _ = make_batch(room)
    client.responses[1] = ([reply(items[0], 201)], None)

    async def pause_in_flight(**kwargs):
        result = await _extract_messages_result(**kwargs)
        await sync_to_async(pause_job, thread_sensitive=False)(batch.job_id)
        return result

    with patch("app_telegram.services.telegram._extract_messages_result", side_effect=pause_in_flight):
        assert run_batch(batch)["status"] == "paused"
    batch.refresh_from_db()
    assert batch.status == ProcessingBatch.PENDING
    assert batch.processed_count == 1
    assert [call["reply_to"] for call in client.calls] == [1]
    resumed = resume_job(batch.job_id)
    assert resumed.status == ProcessingJob.RUNNING
    assert run_batch(batch)["status"] == "comments_scanned"
    assert [call["reply_to"] for call in client.calls] == [1, 2, 3]


def test_credential_change_is_visible_without_calling_wrong_room(parents, client, telegram_auth_factory):
    _, room, items = parents
    batch, _ = make_batch(room)
    replacement = telegram_auth_factory(name="replacement", api_id=33333, api_hash="replacement")
    RoomItem.objects.filter(pk=room.id).update(access_auth=replacement)
    assert run_batch(batch, retries=scan_comments_processing_batch.max_retries)["status"] == "failed"
    assert client.calls == []
    batch.refresh_from_db()
    assert batch.failed_count == 3
    assert all(entry["reason"] == "credential_changed" for entry in batch.processor_metadata["parent_progress"].values())
    new_batch, _ = make_batch(room)
    assert new_batch.job_id != batch.job_id
    assert new_batch.processor_metadata["auth_id"] == str(replacement.id)
    assert run_batch(new_batch)["status"] == "comments_scanned"


def test_credential_change_during_extraction_discards_stale_results(parents, client, telegram_auth_factory):
    _, room, items = parents
    batch, _ = make_batch(room)
    replacement = telegram_auth_factory(name="replacement", api_id=33333, api_hash="replacement")
    client.responses[1] = ([reply(items[0], 201)], None)

    async def change_access(**kwargs):
        result = await _extract_messages_result(**kwargs)
        await sync_to_async(lambda: RoomItem.objects.filter(pk=room.id).update(access_auth=replacement), thread_sensitive=False)()
        return result

    with patch("app_telegram.services.telegram._extract_messages_result", side_effect=change_access):
        assert run_batch(batch, retries=scan_comments_processing_batch.max_retries)["status"] == "failed"
    items[0].refresh_from_db()
    batch.refresh_from_db()
    assert items[0].last_offset == 0
    assert not MessageItem.objects.filter(is_reply=True).exists()
    assert not ProcessingHandoff.objects.filter(source_batch=batch).exists()
    assert batch.processor_metadata["parent_progress"][str(items[0].pk)]["reason"] == "credential_changed"
    assert batch.processor_metadata["parent_progress"][str(items[0].pk)]["fetched_count"] == 0


def test_missing_parent_is_skipped_without_scanning_an_unlisted_parent(parents, client):
    _, room, items = parents
    batch, _ = make_batch(room)
    parent_id = str(items[0].pk)
    items[0].delete()
    assert run_batch(batch)["status"] == "comments_scanned"
    batch.refresh_from_db()
    assert batch.skipped_count == 1
    assert batch.processor_metadata["parent_progress"][parent_id]["reason"] == "parent_missing"
    assert [call["reply_to"] for call in client.calls] == [2, 3]


@pytest.mark.parametrize("input_model, version", [
    (RoomItem, ""), (RoomItem, COMMENTS_MESSAGES_INPUT_VERSION), (MessageItem, ""),
])
def test_worker_rejects_old_room_input(parents, client, input_model, version):
    _, room, _ = parents
    batch, _ = make_batch(room)
    ProcessingBatch.objects.filter(pk=batch.id).update(
        item_ids=[str(room.id)], item_count=1,
        item_content_type=ContentType.objects.get_for_model(input_model), processor_version=version,
    )
    assert run_batch(batch)["status"] == "failed"
    batch.refresh_from_db()
    assert "MessageItem IDs" in batch.last_error
    assert client.calls == []


def test_floodwait_at_parent_limit_still_stops_new_requests(parents, client):
    _, room, items = parents
    client.responses[1] = ([reply(items[0], 201)], FloodWaitError(None, capture=120))
    batch, _ = make_batch(room, options={"max_msgs": 1})
    assert run_batch(batch)["status"] == "waiting_retry"
    batch.refresh_from_db()
    assert batch.status == ProcessingBatch.RETRY_WAIT
    assert batch.processed_count == 1
    assert batch.processor_metadata["parent_progress"][str(items[0].pk)]["state"] == "completed"
    assert [call["reply_to"] for call in client.calls] == [1]


def test_client_startup_floodwait_is_persisted_and_deferred(parents, client):
    auth, room, _ = parents
    batch, _ = make_batch(room)
    before = timezone.now()

    async def flooded_start(self):
        raise FloodWaitError(None, capture=120)

    with patch.object(CommentsClient, "start", flooded_start):
        assert run_batch(batch)["status"] == "waiting_retry"
    auth.refresh_from_db()
    batch.refresh_from_db()
    assert auth.wait_until >= before + timedelta(seconds=120)
    assert batch.status == ProcessingBatch.RETRY_WAIT
    assert batch.run_after >= auth.wait_until + timedelta(seconds=59)
    assert client.calls == []


def test_client_shutdown_error_does_not_fail_confirmed_parent_units(parents, client):
    _, room, _ = parents
    batch, _ = make_batch(room)

    async def broken_exit(self, *args):
        raise RuntimeError("client shutdown failed after all commits")

    with patch.object(CommentsClient, "__aexit__", broken_exit):
        assert run_batch(batch, retries=scan_comments_processing_batch.max_retries)["status"] == "comments_scanned"
    batch.refresh_from_db()
    assert batch.status == ProcessingBatch.COMPLETED
    assert batch.processed_count == 3 and batch.failed_count == 0


def test_configured_three_hundred_parent_limit(settings, parents):
    settings.TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH = 300
    settings.TELEGRAM_SCAN_MAX_INFLIGHT = 1
    _, room, items = parents
    MessageItem.objects.bulk_create([
        MessageItem(room=room, msg_id=index, link=f"{room.link}/{index}")
        for index in range(4, 305)
    ])
    job, _ = make_job([room])
    with patch("app_telegram.services.jobs.dispatch_ready_batches"):
        dispatch_scan_comments_source(job.id)
        batch = job.batches.get()
        assert batch.item_count == len(batch.item_ids) == 300
        assert dispatch_scan_comments_source(job.id) == []


def test_timeout_commits_received_comments_and_retries_remaining(parents, client):
    _, room, items = parents
    client.responses[1] = ([reply(items[0], 201)], None)
    batch, _ = make_batch(room, options={"max_msgs": 2})
    original = CommentsClient.iter_messages

    class SlowIterator(FakeAsyncIterator):
        async def __anext__(self):
            if self.index:
                await asyncio.sleep(1)
            return await super().__anext__()

    def timed_messages(self, **kwargs):
        if kwargs["reply_to"] == 1 and kwargs["offset_id"] == 0:
            self.calls.append(kwargs)
            return SlowIterator([reply(items[0], 201)])
        return original(self, **kwargs)

    with patch.object(CommentsClient, "iter_messages", timed_messages), patch("app_telegram.services.telegram.MAX_WAIT_TIME", 0.02), pytest.raises(Retry):
        run_batch(batch)
    items[0].refresh_from_db()
    batch.refresh_from_db()
    assert items[0].last_offset == 201
    assert batch.processor_metadata["parent_progress"][str(items[0].pk)]["fetched_count"] == 1
    allow_retry(batch)
    client.responses[1] = ([reply(items[0], 202)], None)
    assert run_batch(batch, retries=1)["status"] == "comments_scanned"
    assert client.calls[-1]["limit"] == 1
    assert MessageItem.objects.filter(reply_to_msg=items[0]).count() == 2


def test_soft_timeout_rolls_back_current_parent_and_keeps_previous(parents, client):
    _, room, items = parents
    client.responses[1] = ([reply(items[0], 201)], None)
    client.responses[2] = ([], SoftTimeLimitExceeded())
    batch, _ = make_batch(room)
    with pytest.raises(Retry):
        run_batch(batch)
    batch.refresh_from_db()
    assert batch.processed_count == 1
    assert str(items[1].pk) not in batch.processor_metadata["parent_progress"]
    assert ProcessingHandoff.objects.filter(source_batch=batch).count() == 1
    allow_retry(batch)
    client.responses[2] = ([], None)
    assert run_batch(batch, retries=1)["status"] == "comments_scanned"
    assert [call["reply_to"] for call in client.calls] == [1, 2, 2, 3]
    assert ProcessingHandoff.objects.filter(source_batch=batch).count() == 1


def test_cancellation_after_fetch_rejects_synchronous_parent_save(parents):
    auth, room, items = parents
    batch, _ = make_batch(room)
    token = lease(batch)
    cancelled = Event()

    def cancelled_fetch(*, related_msg, **kwargs):
        related_msg.last_offset = 201
        cancelled.set()
        return ExtractMessagesResult(parent=related_msg)

    with pytest.raises(asyncio.CancelledError):
        _scan_comment_parent(batch.id, token, str(items[0].pk), cancelled_fetch,
                             auth_id=auth.id, max_msgs=100, cancelled=cancelled)
    items[0].refresh_from_db()
    batch.refresh_from_db()
    assert items[0].last_offset == 0
    assert batch.processor_metadata["parent_progress"] == {}


def test_expired_worker_lease_recovery_preserves_parent_progress(parents, client):
    _, room, items = parents
    batch, _ = make_batch(room)
    token = lease(batch)

    def completed_parent(*, related_msg, **kwargs):
        related_msg.last_scan_at = timezone.now()
        return ExtractMessagesResult(parent=related_msg)

    _scan_comment_parent(batch.id, token, str(items[0].pk), completed_parent,
                         auth_id=batch.processor_metadata["auth_id"], max_msgs=100)
    ProcessingBatch.objects.filter(pk=batch.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
    assert reconcile_jobs()["recovered_batches"] == 1
    assert run_batch(batch)["status"] == "comments_scanned"
    assert [call["reply_to"] for call in client.calls] == [2, 3]
    batch.refresh_from_db()
    assert batch.processed_count == 3


def test_room_traversals_share_one_client_and_serialize_parent_requests(settings, client, room_factory, telegram_auth_factory, message_factory):
    settings.TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH = 300
    auth = telegram_auth_factory()
    rooms = []
    for index in range(10):
        room = room_factory(tg_id=100 + index, link=f"https://t.me/room_{index}", access_auth=auth, access_hash=123)
        rooms.append(room)
        for msg_id in (1, 2):
            message_factory(room=room, msg_id=msg_id, link=f"{room.link}/{msg_id}")
    job, _ = make_job(rooms)
    dispatch_scan_comments_source(job.id)
    batch = job.batches.get()
    running = {}
    maximum = 0
    loops = set()

    async def observe_threads(*, room, related_msg, **kwargs):
        nonlocal maximum
        loops.add(asyncio.get_running_loop())
        running[room.id] = running.get(room.id, 0) + 1
        assert running[room.id] == 1
        maximum = max(maximum, sum(running.values()))
        await asyncio.sleep(0)
        running[room.id] -= 1
        related_msg.last_scan_at = timezone.now()
        return ExtractMessagesResult(parent=related_msg)

    with patch("app_telegram.services.telegram._extract_messages_result", side_effect=observe_threads):
        assert run_batch(batch)["status"] == "comments_scanned"
    assert maximum == 1
    assert len(loops) == 1


def test_source_pause_and_resume_before_first_page(parents):
    _, room, _ = parents
    job, _ = make_job([room])
    pause_job(job.pk)
    assert dispatch_scan_comments_source(job.pk) == []
    assert not job.batches.exists()
    assert resume_job(job.pk).status == ProcessingJob.RUNNING
    assert len(dispatch_scan_comments_source(job.pk)) == 1
