import pytest
from rest_framework.test import APIClient

from app_base.models import Query
from app_telegram.models import MessageItem, RoomItem, SeedItem, TelegramAuth
from app_users.models import User


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email="reviewer@example.org",
        username="reviewer",
        password="test-password",
        is_staff=True,
    )


@pytest.fixture
def query(db):
    return Query.objects.create()


@pytest.fixture
def seed_factory(db):
    def create(**overrides):
        defaults = {
            "link": "https://t.me/test_seed",
            "title": "Test seed",
            "tags": ["test"],
            "type": SeedItem.CHANNEL_TYPE,
            "lang": "en",
        }
        defaults.update(overrides)
        return SeedItem.objects.create(**defaults)

    return create


@pytest.fixture
def telegram_auth_factory(db):
    def create(**overrides):
        defaults = {
            "name": "test-auth",
            "phone": "+34123456789",
            "api_id": 12345,
            "api_hash": "test-hash",
            "session": "test-session",
        }
        defaults.update(overrides)
        return TelegramAuth.objects.create(**defaults)

    return create


@pytest.fixture
def room_factory(db):
    def create(**overrides):
        defaults = {
            "tg_id": 10001,
            "unique_name": "test_room",
            "link": "https://t.me/test_room",
            "title": "Test room",
            "is_channel": True,
        }
        defaults.update(overrides)
        return RoomItem.objects.create(**defaults)

    return create


@pytest.fixture
def message_factory(db, room_factory):
    def create(**overrides):
        room = overrides.pop("room", None) or room_factory()
        defaults = {
            "link": f"https://t.me/{room.unique_name}/1",
            "msg_id": 1,
            "room": room,
            "media_type": MessageItem.TEXT,
            "text": "Test message",
            "lang": "en",
        }
        defaults.update(overrides)
        return MessageItem.objects.create(**defaults)

    return create
