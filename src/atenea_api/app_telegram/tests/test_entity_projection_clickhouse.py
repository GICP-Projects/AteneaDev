"""Real PostgreSQL + ClickHouse tests for merges, filters and ambiguous retries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import close_old_connections
from django.test import override_settings

from app_entity.enums import EntityType
from app_entity.models import AnnotatedEntity, EntityItem
from app_telegram import clickhouse as ch
from app_telegram.models import EntityAnalyticsOutbox, MessageItem
from app_telegram.services import index as telegram_index
from app_telegram.services.analytics import entity_ranking, entity_ranking_ordering


pytestmark = [pytest.mark.clickhouse, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def client():
    assert settings.CLICKHOUSE["database"] == "atenea_test"
    ch.ensure_schema()
    client = ch.get_client()
    client.command(f"TRUNCATE TABLE {ch.table_name()}")
    try:
        yield client
    finally:
        client.command(f"SYSTEM START MERGES {ch.table_name()}")
        client.command(f"TRUNCATE TABLE {ch.table_name()}")
        client.close()


def fact(**changes):
    return {"msg_id": str(uuid4()), "entity_id": str(uuid4()), "room_id": str(uuid4()),
            "type": 1, "created_at": "2021-10-15T01:00:00+00:00",
            "stored_date": "2021-10-16T02:00:00+00:00", "is_reply": False, "count": 2, **changes}


def insert(client, rows, token=None):
    ch.insert_rows(client, [{**row, "sign": row.get("sign", 1), "version": row.get("version", 0)} for row in rows],
                   token=token or str(uuid4()))


def test_signed_update_is_exact_before_and_after_merges(client):
    old = fact(count=3)
    client.command(f"SYSTEM STOP MERGES {ch.table_name()}")
    insert(client, [old])
    new = {**old, "count": 7, "version": 1}
    insert(client, [{**old, "sign": -1}, new])
    expected = [{"entity_id": __import__('uuid').UUID(old["entity_id"]), "message_count": 1, "appearance_count": 7}]
    assert entity_ranking({}, client=client) == expected
    client.command(f"SYSTEM START MERGES {ch.table_name()}")
    client.command(f"OPTIMIZE TABLE {ch.table_name()} FINAL")
    assert entity_ranking({}, client=client) == expected


def test_date_room_type_and_reply_changes_cancel_old_dimensions(client, room_factory):
    first = room_factory()
    second = room_factory(tg_id=10002, unique_name="second", link="https://t.me/second")
    old = fact(room_id=str(first.pk), count=3)
    insert(client, [old])
    new = {**old, "room_id": str(second.pk), "created_at": "2022-01-02T00:00:00+00:00", "type": 2, "is_reply": True, "count": 8}
    with ch.projection_lock():
        telegram_index._write_entity_changes(client, [{**ch.normalize_fact(old), "version": 0}], [new])
    assert entity_ranking({"createdat_max": "2021-12-31T23:59:59Z"}, client=client) == []
    assert entity_ranking({"room": ["^test_room$"]}, client=client) == []
    assert entity_ranking({}, entity_type=["ORG"], client=client) == []
    rows = entity_ranking({"room": ["^second$"], "is_reply": True}, client=client)
    assert (rows[0]["message_count"], rows[0]["appearance_count"]) == (1, 8)


def test_timeout_after_insert_replays_exact_block_before_new_work(client):
    wanted = fact()
    original = ch.insert_rows
    recorded = []

    def ambiguous(client, rows, *, token):
        recorded.append((rows, token))
        original(client, rows, token=token)
        raise TimeoutError("response lost after commit")

    with ch.projection_lock(), patch.object(ch, "insert_rows", side_effect=ambiguous):
        with pytest.raises(TimeoutError):
            telegram_index._write_entity_changes(client, [], [wanted])
    pending = EntityAnalyticsOutbox.objects.get()
    assert pending.next_block == 0
    with ch.projection_lock(), patch.object(ch, "insert_rows", wraps=original) as replay:
        telegram_index._drain_entity_outbox(client)
    assert replay.call_args.kwargs["token"] == recorded[0][1]
    assert replay.call_args.args[1] == recorded[0][0]
    assert not EntityAnalyticsOutbox.objects.exists()
    rows = entity_ranking({}, client=client)
    assert rows[0]["message_count"] == 1
    assert rows[0]["appearance_count"] == 2


def test_partial_multiblock_update_recovers_with_original_payload(client):
    old = fact()
    insert(client, [old])
    new = {**old, "count": 9, "created_at": "2026-10-01T00:00:00Z"}
    original = ch.insert_rows
    calls = 0

    def fail_second(client, rows, *, token):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("second partition unavailable")
        return original(client, rows, token=token)

    with override_settings(CLICKHOUSE_BATCH_SIZE=1), ch.projection_lock(), patch.object(ch, "insert_rows", side_effect=fail_second):
        with pytest.raises(TimeoutError):
            telegram_index._write_entity_changes(client, [{**ch.normalize_fact(old), "version": 0}], [new])
    assert EntityAnalyticsOutbox.objects.get().next_block == 1
    with ch.projection_lock():
        telegram_index._drain_entity_outbox(client)
    assert entity_ranking({}, client=client)[0]["appearance_count"] == 9


def test_message_and_entity_deletion_are_idempotent(client):
    first = fact()
    second = fact(msg_id=first["msg_id"])
    third = fact()
    insert(client, [first, second, third])
    telegram_index.delete_entity_facts([first["entity_id"]], client=client)
    assert len(entity_ranking({}, client=client)) == 2
    telegram_index.delete_message_entity_facts([first["msg_id"]], client=client)
    telegram_index.delete_message_entity_facts([first["msg_id"]], client=client)
    assert str(entity_ranking({}, client=client)[0]["entity_id"]) == third["entity_id"]


def test_null_dates_and_inclusive_microsecond_boundaries(client):
    empty = fact(created_at=None, stored_date=None)
    dated = fact(created_at="2026-10-14T23:59:59.999999Z")
    insert(client, [empty, dated])
    assert len(entity_ranking({}, client=client)) == 2
    assert len(entity_ranking({"createdat_min": dated["created_at"], "createdat_max": dated["created_at"]}, client=client)) == 1
    assert entity_ranking({"createdat_min": "2026-10-15T00:00:00Z"}, client=client) == []


def test_uuid_text_ties_and_cursor_pages(client):
    ids = ["00000000-0000-0000-0000-000000000002", "ffffffff-ffff-ffff-0000-000000000001"]
    insert(client, [fact(entity_id=value) for value in ids])
    rows = entity_ranking({}, limit=1, client=client)
    assert str(rows[0]["entity_id"]) == ids[0]
    next_rows = entity_ranking({}, position=[1, 2, ids[0]], client=client)
    assert str(next_rows[0]["entity_id"]) == ids[1]
    previous = entity_ranking({}, position=[1, 2, ids[1]], reverse=True, client=client)
    assert str(previous[0]["entity_id"]) == ids[0]


def test_reconciliation_and_concurrent_retries_do_not_double_count(client, message_factory):
    message = message_factory(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    entity = EntityItem.objects.create(unique_ent="analytics_per", name="Alice", type=EntityType.PER)
    AnnotatedEntity.objects.create(entity=entity, content_type=ContentType.objects.get_for_model(MessageItem),
                                   object_id=message.pk, start_offset=0, end_offset=5)

    def project():
        close_old_connections()
        try:
            return telegram_index.reconcile_message_entities([message.pk])
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: project(), range(2))) == [1, 1]
    assert entity_ranking({}, client=client)[0]["message_count"] == 1
    message_id = message.pk
    with patch("app_scheduler.services.jobs.dispatch_ready_batches"):
        message.delete()
    telegram_index.reconcile_message_entities([message_id], client=client)
    assert entity_ranking({}, client=client) == []


def test_index_counts_include_unchanged_facts_and_remain_stable_on_retry(client, message_factory):
    message = message_factory(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    entity = EntityItem.objects.create(unique_ent="index_count_per", name="Alice", type=EntityType.PER)
    content_type = ContentType.objects.get_for_model(MessageItem)
    for start in (0, 6):
        AnnotatedEntity.objects.create(entity=entity, content_type=content_type, object_id=message.pk,
                                       start_offset=start, end_offset=start + 5)
    expected = {"search_messages_indexed": 0, "analytics_entity_facts_synchronized": 1}
    assert telegram_index.index_message_ids([message.pk], rewrite_messages=False) == expected
    assert telegram_index.index_message_ids([message.pk], rewrite_messages=False) == expected
    rows = entity_ranking({}, client=client)
    assert rows[0]["message_count"] == 1
    assert rows[0]["appearance_count"] == 2


def test_large_uuid_filter_is_uploaded_as_external_data(client):
    row = fact()
    insert(client, [row])
    ids = [str(uuid4()) for _ in range(1001)] + [row["msg_id"]]
    rows = telegram_index._current_entity_facts(client, field="msg_id", ids=ids)
    assert len(rows) == 1
    assert rows[0]["msg_id"] == row["msg_id"]
