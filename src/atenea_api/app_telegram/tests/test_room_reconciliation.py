"""Essential contracts for Room reconciliation, checkpoints and index synchronization."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from django.conf import settings
from django.db import OperationalError, close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from app_scheduler.models import ProcessingBatch, ProcessingJob
from app_scheduler.services.jobs import (
    begin_batch_stage_locked, create_batch_locked, create_processing_job, dispatch_handoffs,
)
from app_telegram.job_keys import (
    TELEGRAM_ROOM_METADATA_PROCESSOR, TELEGRAM_ROOM_METADATA_STAGE,
    TELEGRAM_SCAN_PROCESSOR, TELEGRAM_SCAN_STAGE,
)
from app_telegram.models import MessageItem, RoomItem
from app_telegram.serializers import ScanRoomSerializer
from app_telegram.services import index, jobs, room_reconciliation as reconciliation
from app_telegram.services.index import index_room_ids
from app_telegram.services.jobs import _scan_fingerprint, create_scan_jobs_from_queryset
from app_telegram.services.telegram import _extract_messages_result, populate_seeds, scan_rooms
from app_telegram.tasks import scan_rooms_processing_batch, sync_room_metadata_batch
from app_telegram.tests.test_message_collection import (
    FakeAsyncIterator, FakeTelegramClient, telegram_message,
)


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def external_tasks(monkeypatch):
    monkeypatch.setattr("app_scheduler.services.jobs.current_app.send_task", Mock())
    monkeypatch.setattr("app_telegram.services.telegram.detect_lang", lambda _: "en")


@pytest.fixture
def telegram(monkeypatch):
    class RoomClient(FakeTelegramClient):
        async def __call__(self, request):
            return await self.full_channel(request)

    client = RoomClient()
    client.full_channel = AsyncMock(side_effect=lambda request: SimpleNamespace(
        full_chat=SimpleNamespace(id=request.channel.channel_id, about=""),
    ))
    client.responses = {}
    client.iter_messages = Mock(side_effect=lambda **kwargs: FakeAsyncIterator(
        client.responses.get(kwargs["entity"].channel_id, []),
    ))
    client.get_entity = AsyncMock()
    monkeypatch.setattr("app_telegram.services.telegram.TelegramClient", lambda *a, **k: client)
    monkeypatch.setattr("app_telegram.services.telegram.StringSession", lambda value: value)
    return client


@pytest.fixture
def backends(monkeypatch):
    search, vectors, rooms = Mock(), Mock(), Mock()
    search.options.return_value = search
    search.update_by_query.return_value = {"task": "node:123"}
    search.tasks.get.return_value = {"completed": True, "response": {"updated": 7}}
    search.tasks.list.return_value = {"tasks": []}
    search.indices.refresh.return_value = {"_shards": {"failed": 0}}
    vectors.set_payload.return_value = SimpleNamespace(status="completed")
    monkeypatch.setattr(reconciliation.connections, "get_connection", lambda: search)
    monkeypatch.setattr("app_metadata.vector_store.get_qdrant_client", lambda: vectors)
    monkeypatch.setattr(index, "index_room_ids", rooms)
    return search, vectors, rooms


def observation(room, username, at=None):
    return reconciliation.RoomIdentitySnapshot(
        room.tg_id, username, room.title, at or timezone.now(),
        about=room.about, about_observed=True,
    )


def auth_data(auth):
    return {"pk": auth.pk, "session": auth.session, "api_id": auth.api_id, "api_hash": auth.api_hash}


def start_sync(rooms, fields=None):
    with transaction.atomic():
        for room in rooms:
            reconciliation.mark_room_metadata_pending(
                RoomItem.objects.select_for_update().get(pk=room.pk), fields,
            )
        job = jobs.queue_room_metadata_sync([room.pk for room in rooms])
        batch = job.batches.select_for_update().select_related("job").get()
        token = begin_batch_stage_locked(batch, TELEGRAM_ROOM_METADATA_STAGE)
    return batch, token


def test_check_room_defaults_and_deduplication(api_client, staff_user, monkeypatch):
    serializer = ScanRoomSerializer(data={})
    assert serializer.is_valid() and serializer.validated_data["check_room"] is False
    args = {"processor": TELEGRAM_SCAN_PROCESSOR, "filters": {}}
    baseline = _scan_fingerprint(**args, options={})
    assert baseline == _scan_fingerprint(**args, options={"check_room": False})
    assert baseline != _scan_fingerprint(**args, options={"check_room": True})
    launch = Mock(return_value=1)
    monkeypatch.setattr("app_telegram.views.staff_views.api.room_scanning", launch)
    api_client.force_authenticate(user=staff_user)
    assert api_client.post("/api/v1/tg/room/scan", {"check_room": True}, format="json").status_code == 202
    assert launch.call_args.kwargs["check_room"] is True


@pytest.mark.parametrize("rollback", [False, True])
def test_scan_checkpoint_commits_or_rolls_back_everything(
    room_factory, message_factory, telegram_auth_factory, telegram, monkeypatch, rollback,
):
    auth = telegram_auth_factory()
    room = room_factory(unique_name="before", link="https://t.me/before", access_auth=auth, access_hash=123)
    suffix = "7?comment=8&source=https://t.me/before/99"
    old_prefix = "https://t.me/BeFoRe"
    old = message_factory(room=room, link=f"{old_prefix}/{suffix}", is_reply=True, reply_to_id=6)
    telegram.responses[room.tg_id] = [telegram_message(chat=SimpleNamespace(
        id=room.tg_id, username="after", title=room.title,
    ))]
    root, _, _ = create_scan_jobs_from_queryset(
        RoomItem.objects.filter(pk=room.pk), filters={}, options={"check_room": True},
    )
    if rollback:
        queue = jobs.queue_room_metadata_sync

        def abort_checkpoint(*args, **kwargs):
            assert queue(*args, **kwargs).incoming_handoffs.exists()
            raise RuntimeError("checkpoint aborted")

        monkeypatch.setattr(jobs, "queue_room_metadata_sync", abort_checkpoint)
        with pytest.raises(RuntimeError, match="checkpoint aborted"):
            scan_rooms_processing_batch.run(root.batches.get().pk)
    else:
        scan_rooms_processing_batch.run(root.batches.get().pk)
    room.refresh_from_db()
    old.refresh_from_db()
    assert room.unique_name == ("before" if rollback else "after")
    assert old.link == f"{old_prefix if rollback else 'https://t.me/after'}/{suffix}"
    assert room.last_offset == (0 if rollback else 101)
    assert MessageItem.objects.filter(room=room).count() == (1 if rollback else 2)
    assert bool(room.username_history) is not rollback
    assert bool(room.metadata_sync_pending_at) is not rollback
    children = root.child_jobs.filter(processor=TELEGRAM_ROOM_METADATA_PROCESSOR)
    assert children.count() == int(not rollback)
    if not rollback:
        child = children.get()
        assert child.incoming_handoffs.get().item_count == 1
        assert child.batches.get().item_ids == [str(room.pk)]
        assert MessageItem.objects.get(room=room, msg_id=101).link == "https://t.me/after/101"
    telegram.get_entity.assert_not_awaited()


@pytest.mark.parametrize("case", [
    "disabled", "complete", "empty", "reduced", "wrong_id",
    "missing_title", "missing_username", "private", "unavailable", "interrupted",
])
def test_identity_check_uses_message_or_peer_fallback(room_factory, telegram, case):
    room = room_factory(access_hash=123)
    entity = SimpleNamespace(id=room.tg_id, username="after", title=room.title, min=False)
    chat = SimpleNamespace(**vars(entity))
    if case == "reduced":
        chat.min = True
    elif case == "wrong_id":
        chat.id = room.tg_id + 1
    elif case == "missing_title":
        chat.title = None
    elif case in {"missing_username", "private", "interrupted"}:
        chat.username = None
        if case == "private":
            entity.username = None
    telegram.responses[room.tg_id] = [] if case in {"empty", "unavailable"} else [telegram_message(chat=chat)]
    if case == "interrupted":
        telegram.responses[room.tg_id].append(TimeoutError())
    telegram.get_entity.return_value = entity
    if case == "unavailable":
        telegram.get_entity.side_effect = ConnectionRefusedError("Telegram unavailable")
    result = asyncio.run(_extract_messages_result(telegram, room, 10, check_room=case != "disabled"))
    assert telegram.get_entity.await_count == int(case in {
        "empty", "wrong_id", "missing_title", "missing_username", "private", "unavailable",
    })
    if case == "disabled":
        assert result.room_identity is None and result.messages[0].link == room.link + "/101"
    elif case in {"private", "unavailable", "interrupted"}:
        assert result.room is None and result.messages == [] and room.last_offset == 0
        assert room.is_private is (case == "private")
    else:
        assert result.room_identity.username == "after"
        if telegram.get_entity.await_count:
            peer = telegram.get_entity.call_args.args[0]
            assert peer.channel_id == room.tg_id and peer.access_hash == room.access_hash
    assert telegram.full_channel.await_count == int(case not in {
        "disabled", "private", "unavailable", "interrupted",
    })


@pytest.mark.parametrize(("room_link", "message_link"), [
    ("https://t.me/before", "https://t.me/c/123/456"),
    ("https://t.me/before", "https://t.me/joinchat/token/123"),
    ("https://t.me/before", "https://example.org/before/123"),
    ("https://t.me/joinchat/token", "https://t.me/before/123"),
])
def test_invalid_source_urls_leave_room_and_messages_unchanged(room_factory, message_factory, room_link, message_link):
    room = room_factory(unique_name="before", link=room_link)
    message = message_factory(room=room, link=message_link)
    with pytest.raises(reconciliation.RoomReconciliationError, match="message_link_invalid"):
        with transaction.atomic():
            reconciliation.reconcile_room(room, observation(room, "after"))
    room.refresh_from_db()
    message.refresh_from_db()
    assert room.unique_name == "before" and room.link == room_link
    assert room.username_history == [] and room.metadata_sync_pending_at is None
    assert message.link == message_link


def test_unchanged_checks_history_and_stale_observations(room_factory, monkeypatch):
    room = room_factory(unique_name="alpha", link="https://t.me/alpha")
    at = timezone.now()
    monkeypatch.setattr(reconciliation.timezone, "now", lambda: at)
    with transaction.atomic():
        room, pending = reconciliation.reconcile_room(room, observation(room, "alpha", at))
        assert not pending and room.username_checked_at == at
        assert jobs.queue_room_metadata_sync([room.pk]) is None
        room, _ = reconciliation.reconcile_room(room, observation(room, "beta", at + timedelta(seconds=1)))
        first_mark = room.metadata_sync_pending_at
        room, _ = reconciliation.reconcile_room(room, observation(room, "BETA", at + timedelta(seconds=2)))
        assert room.metadata_sync_pending_at == first_mark
        room, _ = reconciliation.reconcile_room(room, observation(room, "alpha", at + timedelta(seconds=3)))
        room, _ = reconciliation.reconcile_room(room, observation(room, "obsolete", at))
    assert room.unique_name == "alpha" and room.metadata_sync_pending_at > first_mark
    assert [entry["username"] for entry in room.username_history] == ["alpha", "BETA"]
    assert all(set(entry) == {"username", "title", "about", "detected_at"} for entry in room.username_history)
    assert all(entry["title"] is None and entry["about"] is None for entry in room.username_history)
    assert not ProcessingJob.objects.exists()


@pytest.mark.parametrize("changed", [
    {"username"}, {"title"}, {"about"}, {"username", "title"},
    {"username", "about"}, {"title", "about"}, {"username", "title", "about"},
])
def test_history_combines_previous_values_and_retries_do_not_duplicate(room_factory, changed):
    room = room_factory(about="Old about")
    at = timezone.now()
    snapshot = replace(
        observation(room, "new_name" if "username" in changed else room.unique_name, at),
        title="New title" if "title" in changed else room.title,
        about="New about" if "about" in changed else room.about,
    )
    expected = {
        "username": room.unique_name if "username" in changed else None,
        "title": room.title if "title" in changed else None,
        "about": room.about if "about" in changed else None,
        "detected_at": at.isoformat(),
    }
    with transaction.atomic():
        current, pending = reconciliation.reconcile_room(room, snapshot)
        first_mark = current.metadata_sync_pending_at
        current, _ = reconciliation.reconcile_room(current, snapshot)
    room.refresh_from_db()
    assert pending == {str(room.pk)}
    assert room.username_history == [expected]
    assert room.metadata_sync_pending_at == first_mark
    expected_fields = changed - {"username"} | {"username_history"}
    if "username" in changed:
        expected_fields.update({"unique_name", "link"})
    assert set(room.metadata_sync_pending_fields) == expected_fields
    assert (room.unique_name, room.title, room.about) == (snapshot.username, snapshot.title, snapshot.about)


@pytest.mark.parametrize(("old", "new", "expected"), [
    (None, "New description", ""), ("Old description", "", "Old description"), (None, "", None),
])
def test_empty_about_is_known_empty_and_not_an_unchanged_null(room_factory, old, new, expected):
    room = room_factory(about=old)
    snapshot = replace(observation(room, room.unique_name), about=new)
    with transaction.atomic():
        reconciliation.reconcile_room(room, snapshot)
    room.refresh_from_db()
    if expected is None:
        assert room.username_history == [] and room.metadata_sync_pending_at is None
    else:
        assert room.username_history == [{
            "username": None, "title": None, "about": expected,
            "detected_at": snapshot.observed_at.isoformat(),
        }]


@pytest.mark.parametrize("trigger", ["unchanged", "username", "title", "link"])
def test_scan_refreshes_about_only_for_a_detected_identity_change(
    room_factory, telegram_auth_factory, telegram, trigger,
):
    auth = telegram_auth_factory()
    room = room_factory(about="Stored about", access_auth=auth, access_hash=123,
                        link="https://t.me/stale_link" if trigger == "link" else "https://t.me/test_room")
    username = "new_name" if trigger == "username" else room.unique_name
    title = "New title" if trigger == "title" else room.title
    telegram.responses[room.tg_id] = [telegram_message(chat=SimpleNamespace(
        id=room.tg_id, username=username, title=title,
    ))]
    telegram.full_channel.side_effect = None
    telegram.full_channel.return_value = SimpleNamespace(full_chat=SimpleNamespace(id=room.tg_id, about="Fresh about"))
    scan_rooms.run([room.pk], auth_data(auth), check_room=True)
    room.refresh_from_db()
    assert room.about == ("Stored about" if trigger == "unchanged" else "Fresh about")
    assert telegram.full_channel.await_count == int(trigger != "unchanged")
    assert room.last_offset == 101 and room.link == f"https://t.me/{username}"
    if trigger == "unchanged":
        assert room.username_history == [] and not ProcessingJob.objects.exists()
    else:
        entry, = room.username_history
        assert entry["username"] == ("test_room" if trigger == "username" else None)
        assert entry["title"] == ("Test room" if trigger == "title" else None)
        assert entry["about"] == "Stored about"
        assert ProcessingJob.objects.get().processor == TELEGRAM_ROOM_METADATA_PROCESSOR


@pytest.mark.parametrize("failure", ["network", "timeout", "wrong_id", "missing_about"])
def test_scan_about_failure_defers_without_partial_checkpoint(
    room_factory, message_factory, telegram_auth_factory, telegram, failure,
):
    auth = telegram_auth_factory()
    room = room_factory(about="Stored about", access_auth=auth, access_hash=123)
    old = message_factory(room=room)
    telegram.responses[room.tg_id] = [telegram_message(chat=SimpleNamespace(
        id=room.tg_id, username="new_name", title="New title",
    ))]
    if failure in {"network", "timeout"}:
        telegram.full_channel.side_effect = OSError("offline") if failure == "network" else TimeoutError()
    else:
        telegram.full_channel.side_effect = None
        full_chat = SimpleNamespace(id=room.tg_id + 1, about="Fresh about") if failure == "wrong_id" else SimpleNamespace(id=room.tg_id)
        telegram.full_channel.return_value = SimpleNamespace(full_chat=full_chat)
    root, _, _ = create_scan_jobs_from_queryset(
        RoomItem.objects.filter(pk=room.pk), filters={}, options={"check_room": True},
    )
    scan_rooms_processing_batch.run(root.batches.get().pk)
    room.refresh_from_db()
    old.refresh_from_db()
    batch = root.batches.get()
    assert batch.processor_metadata["room_progress"][str(room.pk)]["reason"] == "room_about_unavailable"
    assert batch.deferred_count == 1 and batch.processed_count == 0
    assert (room.unique_name, room.title, room.about) == ("test_room", "Test room", "Stored about")
    assert room.last_offset == 0 and room.last_offset_update is None
    assert room.last_scan_at is None and room.username_checked_at is None
    assert room.username_history == [] and room.metadata_sync_pending_at is None
    assert list(MessageItem.objects.filter(room=room).values_list("pk", flat=True)) == [old.pk]
    assert old.link == "https://t.me/test_room/1"
    assert not root.child_jobs.filter(processor=TELEGRAM_ROOM_METADATA_PROCESSOR).exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_reused_username_is_reclaimed_only_with_checks_enabled(
    room_factory, message_factory, telegram_auth_factory, telegram, enabled,
):
    auth = telegram_auth_factory()
    owner = room_factory(tg_id=10, unique_name="current", link="https://t.me/current", access_auth=auth, access_hash=123)
    old = message_factory(room=owner, msg_id=101, link="https://t.me/reused/101")
    room = room_factory(tg_id=20, unique_name="reused", link="https://t.me/reused", access_auth=auth, access_hash=123)
    telegram.responses[room.tg_id] = [telegram_message(chat=SimpleNamespace(
        id=room.tg_id, username=room.unique_name, title=room.title,
    ))]
    telegram.get_entity.return_value = SimpleNamespace(id=owner.tg_id, username="current", title=owner.title)
    scan_rooms.run([room.pk], auth_data(auth), check_room=enabled)
    room.refresh_from_db()
    owner.refresh_from_db()
    old.refresh_from_db()
    assert room.last_offset == (101 if enabled else 0)
    assert old.link == ("https://t.me/current/101" if enabled else "https://t.me/reused/101")
    assert telegram.get_entity.await_count == int(enabled)
    assert bool(owner.metadata_sync_pending_at) is enabled
    if enabled:
        assert owner.username_history[0]["username"] == "reused"
        assert owner.username_history[0]["title"] is None and owner.username_history[0]["about"] is None
    telegram.full_channel.assert_not_awaited()


@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.parametrize("title_change", [False, True])
def test_populate_reuses_its_response_and_marks_final_metadata(
    room_factory, seed_factory, telegram_auth_factory, monkeypatch, rename, title_change,
):
    from app_telegram.tests.test_collection_pipeline import FakeChannel, FakeTelegramClient as PopulateClient

    entity, auth, client = FakeChannel(), telegram_auth_factory(), PopulateClient()
    username = "before" if rename else entity.username
    room = room_factory(tg_id=entity.id, unique_name=username, link=f"https://t.me/{username}",
                        title="Previous title" if title_change else entity.title,
                        about="Previous description", access_auth=auth, access_hash=123)
    seed = seed_factory(link=f"https://t.me/{entity.username}")
    client.get_entity = AsyncMock(return_value=entity)
    monkeypatch.setattr("app_telegram.services.telegram.TelegramClient", lambda *a, **k: client)
    monkeypatch.setattr("app_telegram.services.telegram.Channel", FakeChannel)
    monkeypatch.setattr("app_telegram.services.telegram.StringSession", lambda value: value)
    populate_seeds.run([seed.pk], auth_data(auth))
    room.refresh_from_db()
    assert room.unique_name == entity.username and room.about != "Previous description"
    entry, = room.username_history
    assert entry["username"] == ("before" if rename else None)
    assert entry["title"] == ("Previous title" if title_change else None)
    assert entry["about"] == "Previous description" and room.metadata_sync_pending_at is not None
    assert ProcessingJob.objects.filter(processor=TELEGRAM_ROOM_METADATA_PROCESSOR).count() == 1
    client.get_entity.assert_awaited_once_with(seed.link)


@pytest.mark.parametrize("fields", [set(), {"tags"}, {"lang"}, {"is_channel"}, {"created_at"}, {"tags", "lang", "is_channel", "created_at"}])
def test_populate_marks_only_changed_extra_metadata(
    room_factory, seed_factory, telegram_auth_factory, monkeypatch, backends, fields,
):
    from app_telegram.tests.test_collection_pipeline import FakeChannel, FakeTelegramClient as PopulateClient

    entity, auth, client = FakeChannel(), telegram_auth_factory(), PopulateClient()
    seed = seed_factory(link=f"https://t.me/{entity.username}")
    old = {
        "tags": ["old"] if "tags" in fields else seed.tags,
        "lang": "es" if "lang" in fields else seed.lang,
        "is_channel": "is_channel" not in fields,
        "created_at": entity.date - timedelta(days=1) if "created_at" in fields else entity.date,
    }
    room = room_factory(
        tg_id=entity.id, unique_name=entity.username, link=seed.link,
        title=entity.title, about="Collected about", **old,
    )
    monkeypatch.setattr("app_telegram.services.telegram.TelegramClient", lambda *a, **k: client)
    monkeypatch.setattr("app_telegram.services.telegram.Channel", FakeChannel)
    monkeypatch.setattr("app_telegram.services.telegram.StringSession", lambda value: value)
    with CaptureQueriesContext(connection) as queries:
        populate_seeds.run([seed.pk], auth_data(auth))
    assert not any('"app_telegram_messageitem"' in query["sql"] for query in queries)
    room.refresh_from_db()
    assert set(room.metadata_sync_pending_fields) == fields
    assert room.username_history == []
    if not fields:
        assert room.metadata_sync_pending_at is None
        assert not ProcessingJob.objects.filter(processor=TELEGRAM_ROOM_METADATA_PROCESSOR).exists()
    else:
        batch, token = start_sync([room], [])
        assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
        assert backends[0].update_by_query.call_count == int("tags" in fields)
        backends[1].set_payload.assert_not_called()


def test_concurrent_checkpoints_share_child_and_pack_twenty_plus_one(room_factory):
    rooms = [room_factory(tg_id=200+i, unique_name=f"room_{i}", link=f"https://t.me/room_{i}",
                          metadata_sync_pending_at=timezone.now()) for i in range(21)]
    with transaction.atomic():
        root, _ = create_processing_job(
            processor=TELEGRAM_SCAN_PROCESSOR, mode=ProcessingJob.LIVE,
            workflow=[TELEGRAM_SCAN_STAGE], batch_size=100, max_inflight_batches=2, deduplicate=False,
        )
        source = create_batch_locked(root, item_ids=[str(room.pk) for room in rooms], schedule_dispatch=False)
    ready = Barrier(2)

    def enqueue(group):
        close_old_connections()
        try:
            ready.wait(timeout=10)
            return jobs.queue_room_metadata_sync([room.pk for room in group], source_batch_id=source.pk).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        child_ids = list(pool.map(enqueue, [rooms[:8], rooms[8:]]))
    assert len(set(child_ids)) == root.child_jobs.count() == 1
    child = root.child_jobs.get()
    jobs.queue_room_metadata_sync([room.pk for room in rooms], source_batch_id=source.pk)
    assert child.incoming_handoffs.count() == 2
    assert child.batch_size == 20 and child.max_inflight_batches == 2
    ProcessingBatch.objects.filter(pk=source.pk).update(status=ProcessingBatch.COMPLETED)
    dispatch_handoffs(child.pk)
    assert sorted(child.batches.values_list("item_count", flat=True)) == [1, 20]


def test_comments_use_room_link_at_persistence_time(room_factory, message_factory, telegram_auth_factory):
    from app_telegram.services.telegram import ExtractMessagesResult, _scan_comment_parent

    auth = telegram_auth_factory()
    room = room_factory(access_auth=auth, access_hash=123)
    parent = message_factory(room=room)

    def extract(room, related_msg, max_msgs):
        comment = MessageItem(room=room, msg_id=80, is_reply=True, reply_to_id=79,
                              reply_to_msg=related_msg, link=f"{room.link}/1?comment=80")
        with transaction.atomic():
            reconciliation.reconcile_room(room, observation(room, "after"))
        return ExtractMessagesResult(messages=[comment], parent=related_msg, room=room, fetched_count=1)

    result = _scan_comment_parent(None, None, parent.pk, extract, auth_id=auth.pk, max_msgs=100)
    assert MessageItem.objects.get(pk=result["message_ids"][0]).link == "https://t.me/after/1?comment=80"


def test_bulk_sync_filters_by_room_and_preserves_vectors(room_factory, backends):
    room = room_factory(about="Current description", tags=["current"])
    batch, token = start_sync([room])
    search, vectors, rooms = backends
    with CaptureQueriesContext(connection) as queries:
        summary = reconciliation.project_room_metadata(batch.pk, token)
    assert not any('"app_telegram_messageitem"' in query["sql"] for query in queries)
    room.refresh_from_db()
    assert room.metadata_sync_pending_at is None and summary.metadata["pending_rooms"] == 0
    assert summary.processed[0].count == 1 and summary.outputs[0].count == 7
    request = search.update_by_query.call_args.kwargs
    assert request["query"] == {"term": {"room_id": str(room.pk)}}
    assert request["script"]["params"] == {"name": room.unique_name, "about": room.about, "tags": room.tags}
    assert request["wait_for_completion"] is False
    rooms.assert_called_once_with([str(room.pk)])
    assert vectors.set_payload.call_count == len(set(settings.QDRANT.collections.values()))
    for call in vectors.set_payload.call_args_list:
        condition = call.kwargs["points"].must[0]
        assert condition.key == "room_id" and condition.match.value == str(room.pk)
        assert call.kwargs["payload"] == {"room_unique_name": room.unique_name}
        assert call.kwargs["wait"] is True
    vectors.retrieve.assert_not_called()
    vectors.upsert.assert_not_called()


@pytest.mark.parametrize(("fields", "search_needed", "vectors_needed"), [
    ({"title"}, False, False),
    ({"about"}, True, False),
    ({"tags"}, True, False),
    ({"unique_name"}, True, True),
    ({"link"}, False, False),
    ({"lang"}, False, False),
    ({"is_channel"}, False, False),
    ({"created_at"}, False, False),
    ({"username_history"}, False, False),
    ({"title", "username_history"}, False, False),
    ({"title", "about", "tags"}, True, False),
    ({"title", "unique_name"}, True, True),
    ({"unique_name", "about", "tags", "title", "lang"}, True, True),
])
def test_sync_only_calls_affected_stores(room_factory, backends, fields, search_needed, vectors_needed):
    room = room_factory()
    batch, token = start_sync([room], fields)
    search, vectors, rooms = backends
    with CaptureQueriesContext(connection) as queries:
        summary = reconciliation.project_room_metadata(batch.pk, token)
    assert not any('"app_telegram_messageitem"' in query["sql"] for query in queries)
    rooms.assert_called_once_with([str(room.pk)])
    assert search.update_by_query.call_count == int(search_needed)
    assert search.indices.refresh.call_count == int(search_needed)
    assert search.tasks.get.call_count == int(search_needed)
    search.tasks.list.assert_not_called()
    assert vectors.set_payload.call_count == (len(set(settings.QDRANT.collections.values())) if vectors_needed else 0)
    if not vectors_needed:
        vectors.close.assert_not_called()
    assert summary.metadata["pending_rooms"] == 0
    assert summary.metadata["synchronized_rooms"] == 1
    assert summary.outputs[0].count == (7 if search_needed else 0)
    room.refresh_from_db()
    assert room.metadata_sync_pending_at is None and room.metadata_sync_pending_fields == []
    batch.refresh_from_db()
    entry = batch.processor_metadata["room_progress"][str(room.pk)]
    assert entry["state"] == "completed"
    if not search_needed:
        assert entry["es_task_id"] is None and entry["es_request_id"] is None
        assert entry["search_messages_updated"] == 0


@pytest.mark.parametrize("fields", [{"title"}, {"about"}, {"title", "about"}])
def test_reconciliation_without_rename_never_queries_postgres_messages(room_factory, message_factory, fields):
    room = room_factory(about="Before")
    message_factory(room=room)
    snapshot = replace(
        observation(room, room.unique_name),
        title="After" if "title" in fields else room.title,
        about="After" if "about" in fields else room.about,
    )
    with CaptureQueriesContext(connection) as queries, transaction.atomic():
        current, pending = reconciliation.reconcile_room(room, snapshot)
    assert not any('"app_telegram_messageitem"' in query["sql"] for query in queries)
    assert pending == {str(room.pk)}
    assert set(current.metadata_sync_pending_fields) == fields | {"username_history"}


@pytest.mark.parametrize("known_alias", [False, True])
def test_url_repair_only_indexes_room_when_history_changes(room_factory, message_factory, backends, known_alias):
    history = [{"username": "old_alias", "title": None, "about": None, "detected_at": timezone.now().isoformat()}]
    room = room_factory(username_history=history if known_alias else [])
    message = message_factory(room=room, link="https://t.me/old_alias/1?comment=2")
    with transaction.atomic():
        current, pending = reconciliation.reconcile_room(room, observation(room, room.unique_name), force=True)
    message.refresh_from_db()
    assert message.link == f"{room.link}/1?comment=2"
    if known_alias:
        assert pending == set() and current.metadata_sync_pending_at is None
        assert current.metadata_sync_pending_fields == []
        assert jobs.queue_room_metadata_sync([room.pk]) is None
    else:
        assert pending == {str(room.pk)} and current.metadata_sync_pending_fields == ["username_history"]
        batch, token = start_sync([room], [])
        assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
        backends[2].assert_called_once_with([str(room.pk)])
    backends[0].update_by_query.assert_not_called()
    backends[1].set_payload.assert_not_called()


def test_pending_username_followed_by_title_keeps_both_changes(room_factory, backends):
    room = room_factory()
    with transaction.atomic():
        current, _ = reconciliation.reconcile_room(room, observation(room, "after"))
        first_stamp = current.metadata_sync_pending_at
        current, _ = reconciliation.reconcile_room(current, replace(observation(current, "after"), title="After"))
    assert current.metadata_sync_pending_at > first_stamp
    assert set(current.metadata_sync_pending_fields) == {"unique_name", "link", "title", "username_history"}
    batch, token = start_sync([room], [])
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    backends[0].update_by_query.assert_called_once()
    assert backends[1].set_payload.call_count == len(set(settings.QDRANT.collections.values()))


@pytest.mark.parametrize("later_title", [False, True])
def test_legacy_pending_without_fields_syncs_all_stores_once(room_factory, backends, later_title):
    room = room_factory(metadata_sync_pending_at=timezone.now())
    assert room.metadata_sync_pending_fields == []
    if later_title:
        with transaction.atomic():
            current, _ = reconciliation.reconcile_room(room, replace(observation(room, room.unique_name), title="After"))
        assert set(current.metadata_sync_pending_fields) == reconciliation.MESSAGE_METADATA_FIELDS | {"title", "username_history"}
    batch, token = start_sync([room], [])
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    backends[0].update_by_query.assert_called_once()
    assert backends[1].set_payload.call_count == len(set(settings.QDRANT.collections.values()))
    room.refresh_from_db()
    assert room.metadata_sync_pending_at is None and room.metadata_sync_pending_fields == []


def test_empty_changed_fields_do_not_create_work_or_reuse_stale_fields(room_factory):
    room = room_factory(metadata_sync_pending_fields=["unique_name"])
    with transaction.atomic():
        assert reconciliation.mark_room_metadata_pending(room, []) is None
        reconciliation.mark_room_metadata_pending(room, {"title"})
    room.refresh_from_db()
    assert room.metadata_sync_pending_fields == ["title"]


def test_title_only_job_finishes_without_message_or_vector_tasks(room_factory, backends):
    room = room_factory()
    with transaction.atomic():
        reconciliation.reconcile_room(room, replace(observation(room, room.unique_name), title="After"))
    job = jobs.queue_room_metadata_sync([room.pk])
    assert sync_room_metadata_batch.run(job.batches.get().pk)["status"] == "indexed"
    job.refresh_from_db()
    assert job.status == ProcessingJob.COMPLETED
    backends[2].assert_called_once_with([str(room.pk)])
    backends[0].update_by_query.assert_not_called()
    backends[0].tasks.get.assert_not_called()
    backends[1].set_payload.assert_not_called()


def test_title_room_index_failure_retains_fields_for_retry(room_factory, backends):
    room = room_factory()
    batch, token = start_sync([room], {"title"})
    backends[2].side_effect = [RuntimeError("Room index unavailable"), None]
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["failed_rooms"] == 1
    room.refresh_from_db()
    assert room.metadata_sync_pending_at is not None and room.metadata_sync_pending_fields == ["title"]
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    backends[0].update_by_query.assert_not_called()
    backends[1].set_payload.assert_not_called()


def test_title_sync_with_real_room_indexer_never_reads_messages(room_factory, backends, monkeypatch):
    room = room_factory()
    batch, token = start_sync([room], {"title"})
    monkeypatch.setattr(index, "index_room_ids", index_room_ids)
    write = Mock()
    monkeypatch.setattr(index, "_write_actions", write)
    with CaptureQueriesContext(connection) as queries:
        summary = reconciliation.project_room_metadata(batch.pk, token)
    assert summary.metadata["pending_rooms"] == 0
    assert not any('"app_telegram_messageitem"' in query["sql"] for query in queries)
    write.assert_called_once()
    actions = write.call_args.args[1]
    assert len(actions) == 1 and str(actions[0]["_id"]) == str(room.pk)
    backends[0].update_by_query.assert_not_called()
    backends[0].indices.refresh.assert_called_once_with(index="test_room")
    backends[1].set_payload.assert_not_called()


def test_about_change_during_title_sync_keeps_new_work(room_factory, backends):
    room = room_factory()
    batch, token = start_sync([room], {"title"})

    def update_about(room_ids):
        with transaction.atomic():
            current = RoomItem.objects.get(pk=room.pk)
            reconciliation.reconcile_room(current, replace(observation(current, current.unique_name), about="After"))

    backends[2].side_effect = update_about
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 1
    room.refresh_from_db()
    assert set(room.metadata_sync_pending_fields) == {"title", "about", "username_history"}
    backends[0].update_by_query.assert_not_called()
    backends[2].side_effect = None
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    backends[0].update_by_query.assert_called_once()
    backends[1].set_payload.assert_not_called()
    room.refresh_from_db()
    assert room.metadata_sync_pending_at is None and room.metadata_sync_pending_fields == []


def test_async_and_failed_rooms_retry_without_repeating_successes(room_factory, backends, monkeypatch):
    rooms = [room_factory(tg_id=300+i, unique_name=f"room_{i}", link=f"https://t.me/room_{i}") for i in range(3)]
    batch, token = start_sync(rooms)
    search, _, _ = backends
    completed = {"completed": True, "response": {"updated": 7}}
    search.tasks.get.side_effect = [{"completed": False}, completed, completed, completed]
    attempts = []

    def patch_vectors(room_id, username):
        attempts.append(room_id)
        assert not connection.in_atomic_block
        if room_id == batch.item_ids[1] and attempts.count(room_id) == 1:
            raise RuntimeError("Qdrant unavailable")

    monkeypatch.setattr(reconciliation, "sync_room_vector_metadata", patch_vectors)
    first = reconciliation.project_room_metadata(batch.pk, token)
    assert first.metadata["pending_rooms"] == 2 and first.metadata["failed_rooms"] == 1
    assert first.metadata["synchronized_rooms"] == 1
    second = reconciliation.project_room_metadata(batch.pk, token)
    assert second.metadata["pending_rooms"] == 0 and second.processed[0].count == 3
    assert search.update_by_query.call_count == 3
    assert attempts.count(batch.item_ids[1]) == 2 and attempts.count(batch.item_ids[2]) == 1


@pytest.mark.parametrize("failure", [{"timed_out": True}, {"version_conflicts": 1}, {"failures": ["unavailable"]}])
@pytest.mark.parametrize("fields", [{"about"}, {"unique_name"}])
def test_incomplete_search_update_keeps_room_pending(room_factory, backends, failure, fields):
    room = room_factory()
    batch, token = start_sync([room], fields)
    search, vectors, _ = backends
    search.tasks.get.return_value = {"completed": True, "response": failure}
    summary = reconciliation.project_room_metadata(batch.pk, token)
    room.refresh_from_db()
    assert summary.metadata["failed_rooms"] == 1 and room.metadata_sync_pending_at is not None
    assert set(room.metadata_sync_pending_fields) == fields
    vectors.set_payload.assert_not_called()
    search.tasks.get.return_value = {"completed": True, "response": {"updated": 7}}
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    assert search.update_by_query.call_count == 2
    assert vectors.set_payload.call_count == (len(set(settings.QDRANT.collections.values())) if "unique_name" in fields else 0)


def test_qdrant_failure_then_title_change_preserves_username_work(room_factory, backends, monkeypatch):
    room = room_factory()
    batch, token = start_sync([room], {"unique_name"})
    vectors = Mock(side_effect=[RuntimeError("Qdrant unavailable"), None])
    monkeypatch.setattr(reconciliation, "sync_room_vector_metadata", vectors)
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["failed_rooms"] == 1
    room.refresh_from_db()
    assert room.metadata_sync_pending_fields == ["unique_name"]
    with transaction.atomic():
        current, _ = reconciliation.reconcile_room(room, replace(observation(room, room.unique_name), title="After"))
    assert set(current.metadata_sync_pending_fields) == {"unique_name", "title", "username_history"}
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    assert vectors.call_count == 2 and backends[0].update_by_query.call_count == 2
    room.refresh_from_db()
    assert room.metadata_sync_pending_at is None and room.metadata_sync_pending_fields == []


def test_lost_submission_response_recovers_registered_task(room_factory, backends):
    batch, token = start_sync([room_factory()])
    search, _, _ = backends
    search.update_by_query.side_effect = ConnectionError("response lost")
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["failed_rooms"] == 1
    batch.refresh_from_db()
    request_id = batch.processor_metadata["room_progress"][batch.item_ids[0]]["es_request_id"]
    search.tasks.list.return_value = {"tasks": [{"node": "node", "id": 123, "headers": {"X-Opaque-Id": request_id}}]}
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    search.update_by_query.assert_called_once()


def test_second_rename_during_sync_preserves_new_pending_mark(room_factory, backends, monkeypatch):
    room = room_factory()
    batch, token = start_sync([room])
    writes = []

    def rename_during_write(room_id, username):
        writes.append(username)
        if username != "after":
            with transaction.atomic():
                reconciliation.reconcile_room(room, observation(room, "after"))

    monkeypatch.setattr(reconciliation, "sync_room_vector_metadata", rename_during_write)
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 1
    room.refresh_from_db()
    assert room.metadata_sync_pending_at is not None
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    room.refresh_from_db()
    assert room.metadata_sync_pending_at is None and writes == ["test_room", "after"]
    assert backends[0].update_by_query.call_count == 2


def test_normal_indexer_waits_for_old_task_and_reads_current_metadata(room_factory, message_factory, backends, monkeypatch):
    room = room_factory()
    message = message_factory(room=room)
    batch, token = start_sync([room])
    search, _, _ = backends
    search.tasks.get.return_value = {"completed": False}
    reconciliation.project_room_metadata(batch.pk, token)
    with transaction.atomic():
        reconciliation.reconcile_room(room, observation(room, "after"))
    write = Mock()
    monkeypatch.setattr(index, "_write_actions", write)
    with pytest.raises(reconciliation.RoomMetadataWaiting):
        index.index_message_ids([message.pk], sync_entities=False)
    write.assert_not_called()
    search.tasks.get.return_value = {"completed": True, "response": {"updated": 1}}
    index.index_message_ids([message.pk], sync_entities=False)
    assert write.call_args.args[1][0]["_source"]["room_name"] == "after"


def test_busy_index_writer_and_message_row_defer_without_mutation(room_factory, message_factory):
    room = room_factory()
    message = message_factory(room=room)
    ready, release = Event(), Event()

    def hold():
        close_old_connections()
        try:
            with reconciliation.room_metadata_write_lock([room.pk]), transaction.atomic():
                MessageItem.objects.select_for_update().get(pk=message.pk)
                ready.set()
                release.wait(10)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold)
        assert ready.wait(5)
        try:
            with pytest.raises(reconciliation.RoomMetadataWaiting), reconciliation.room_metadata_write_lock([room.pk]):
                pytest.fail("two writers acquired the same Room")
            with pytest.raises(OperationalError), transaction.atomic():
                reconciliation.reconcile_room(room, observation(room, "after"))
        finally:
            release.set()
        future.result(timeout=10)
    room.refresh_from_db()
    assert room.unique_name == "test_room" and room.metadata_sync_pending_at is None
    with reconciliation.room_metadata_write_lock([room.pk]):
        pass


def test_legacy_batch_ignores_cursor_and_creates_pending_mark(room_factory, backends):
    room = room_factory()
    batch, token = start_sync([room])
    RoomItem.objects.filter(pk=room.pk).update(metadata_sync_pending_at=None)
    ProcessingJob.objects.filter(pk=batch.job_id).update(filters={})
    ProcessingBatch.objects.filter(pk=batch.pk).update(processor_metadata={"cursor": "old-message"})
    assert reconciliation.project_room_metadata(batch.pk, token).metadata["pending_rooms"] == 0
    batch.refresh_from_db()
    assert batch.processor_metadata["cursor"] == "old-message"
    backends[0].update_by_query.assert_called_once()


def test_task_defers_while_search_is_running(room_factory, backends):
    room = room_factory(metadata_sync_pending_at=timezone.now())
    job = jobs.queue_room_metadata_sync([room.pk])
    batch = job.batches.get()
    backends[0].tasks.get.return_value = {"completed": False}
    assert sync_room_metadata_batch.run(batch.pk)["status"] == "waiting_retry"
    batch.refresh_from_db()
    assert batch.status == ProcessingBatch.RETRY_WAIT and batch.failed_count == 0
    assert batch.processor_metadata["room_progress"][str(room.pk)]["es_task_id"] == "node:123"
