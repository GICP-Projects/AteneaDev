from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from app_telegram.models import RoomItem, SeedItem
from app_telegram.services.pipelines import (
    load_balancer_split_by_auth,
    populate_pipeline,
)
from app_telegram.services.telegram import populate_seeds


@pytest.mark.django_db
def test_load_balancer_returns_no_work_without_valid_credentials(seed_factory):
    seed = seed_factory()
    task = Mock()

    tasks, assigned = load_balancer_split_by_auth(
        task,
        SeedItem.objects.filter(pk=seed.pk).values_list("pk", flat=True),
        [],
        {},
    )

    assert tasks == []
    assert assigned == []
    task.s.assert_not_called()


@pytest.mark.django_db
def test_load_balancer_limits_each_credential_to_two_hundred_items(
    telegram_auth_factory,
):
    telegram_auth_factory(wait_until=timezone.now())
    seeds = SeedItem.objects.bulk_create(
        [SeedItem(link=f"https://t.me/load_{index}") for index in range(201)]
    )
    task = Mock()
    task.s.return_value = "signature"

    tasks, assigned = load_balancer_split_by_auth(
        task,
        SeedItem.objects.filter(pk__in=[seed.pk for seed in seeds])
        .order_by("pk")
        .values_list("pk", flat=True),
        [],
        {"token": "token"},
    )

    assert tasks == ["signature"]
    assert len(assigned) == 200
    assert len(task.s.call_args.args[0]) == 200


@pytest.mark.django_db
def test_populate_pipeline_schedules_only_valid_unseeded_seeds(
    seed_factory,
    telegram_auth_factory,
):
    telegram_auth_factory(wait_until=timezone.now())
    valid = seed_factory(link="https://t.me/to_populate")
    seed_factory(link="https://t.me/already_seeded", is_seeded=True)
    seed_factory(link="https://t.me/invalid_seed", is_valid=False)

    with (
        patch("app_telegram.services.pipelines.group") as group,
        patch("app_telegram.services.pipelines.bulk_add_query_relationships_with_pks"),
    ):
        group.return_value.apply_async.return_value = None
        count = populate_pipeline()

    assert count == 1
    scheduled_group = group.call_args.args[0]
    assert len(scheduled_group) == 1
    assert str(valid.pk) in repr(scheduled_group[0])


class FakeChannel:
    def __init__(self):
        self.id = 987654
        self.access_hash = 123456
        self.username = "collected_channel"
        self.title = "Collected channel"
        self.date = datetime(2024, 1, 1, tzinfo=dt_timezone.utc)
        self.megagroup = False


class FakeTelegramClient:
    entity = FakeChannel()

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def start(self):
        return self

    async def get_entity(self, link):
        return self.entity

    async def __call__(self, request):
        return SimpleNamespace(full_chat=SimpleNamespace(about="Collected about"))


@pytest.mark.django_db(transaction=True)
def test_populate_seeds_creates_room_without_network(seed_factory, telegram_auth_factory):
    auth = telegram_auth_factory()
    seed = seed_factory(link="https://t.me/collected_channel")
    auth_data = {
        "pk": auth.pk,
        "name": auth.name,
        "api_id": auth.api_id,
        "api_hash": auth.api_hash,
        "session": auth.session,
    }

    with (
        patch("app_telegram.services.telegram.TelegramClient", FakeTelegramClient),
        patch("app_telegram.services.telegram.Channel", FakeChannel),
        patch("app_telegram.services.telegram.StringSession", return_value="session"),
    ):
        result = populate_seeds([seed.pk], auth_data)

    seed.refresh_from_db()
    room = RoomItem.objects.get(seed_item=seed)
    assert seed.is_seeded is True
    assert seed.is_valid is True
    assert room.unique_name == "collected_channel"
    assert result["RoomItem"]["and_filter_fields"]["pk__in"] == [room.pk]


@pytest.mark.django_db(transaction=True)
def test_populate_seeds_marks_unknown_entity_as_invalid(seed_factory, telegram_auth_factory):
    auth = telegram_auth_factory()
    seed = seed_factory(link="https://t.me/invalid_entity")
    auth_data = {
        "pk": auth.pk,
        "name": auth.name,
        "api_id": auth.api_id,
        "api_hash": auth.api_hash,
        "session": auth.session,
    }
    client = type("UnknownEntityClient", (FakeTelegramClient,), {"entity": object()})

    with (
        patch("app_telegram.services.telegram.TelegramClient", client),
        patch("app_telegram.services.telegram.StringSession", return_value="session"),
        patch("app_telegram.services.telegram.asyncio.sleep", return_value=None),
    ):
        result = populate_seeds([seed.pk], auth_data)

    seed.refresh_from_db()
    assert result == {}
    assert seed.is_seeded is False
    assert seed.is_valid is False


class FakeFloodWaitError(Exception):
    def __init__(self, seconds=45):
        self.seconds = seconds


class FakeAuthKeyError(Exception):
    pass


class FloodWaitClient(FakeTelegramClient):
    async def get_entity(self, link):
        raise FakeFloodWaitError()


class ExpiredAuthClient(FakeTelegramClient):
    async def get_entity(self, link):
        raise FakeAuthKeyError("expired session")


@pytest.mark.django_db(transaction=True)
def test_populate_seeds_records_flood_wait(seed_factory, telegram_auth_factory):
    auth = telegram_auth_factory()
    seed = seed_factory(link="https://t.me/flood_wait")
    original_wait = auth.wait_until
    auth_data = {
        "pk": auth.pk,
        "name": auth.name,
        "api_id": auth.api_id,
        "api_hash": auth.api_hash,
        "session": auth.session,
    }

    with (
        patch("app_telegram.services.telegram.TelegramClient", FloodWaitClient),
        patch("app_telegram.services.telegram.StringSession", return_value="session"),
        patch("app_telegram.services.telegram.FloodWaitError", FakeFloodWaitError),
    ):
        result = populate_seeds([seed.pk], auth_data)

    auth.refresh_from_db()
    seed.refresh_from_db()
    assert result == {}
    assert auth.wait_until > original_wait
    assert seed.is_valid is True
    assert seed.is_seeded is False


@pytest.mark.django_db(transaction=True)
def test_populate_seeds_invalidates_expired_credential(seed_factory, telegram_auth_factory):
    auth = telegram_auth_factory()
    seed = seed_factory(link="https://t.me/expired_auth")
    auth_data = {
        "pk": auth.pk,
        "name": auth.name,
        "api_id": auth.api_id,
        "api_hash": auth.api_hash,
        "session": auth.session,
    }

    with (
        patch("app_telegram.services.telegram.TelegramClient", ExpiredAuthClient),
        patch("app_telegram.services.telegram.StringSession", return_value="session"),
        patch("app_telegram.services.telegram.AuthKeyError", FakeAuthKeyError),
    ):
        result = populate_seeds([seed.pk], auth_data)

    auth.refresh_from_db()
    seed.refresh_from_db()
    assert result == {}
    assert auth.is_valid is False
    assert seed.is_valid is True
    assert seed.is_seeded is False
