import asyncio
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from telethon.tl.types import MessageReplyStoryHeader, PeerChannel

from app_base.models import QueryItem
from app_telegram.models import MessageItem
from app_telegram.services.telegram import extract_msgs, scan_comments, scan_rooms


class FakeAsyncIterator:
    def __init__(self, items=(), exception=None):
        self.items = list(items)
        self.exception = exception
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index < len(self.items):
            item = self.items[self.index]
            self.index += 1
            return item
        if self.exception is not None:
            exception, self.exception = self.exception, None
            raise exception
        raise StopAsyncIteration


def telegram_message(msg_id=101, text="Hello **world**", **overrides):
    defaults = {
        "id": msg_id,
        "text": text,
        "date": datetime(2024, 1, 1, tzinfo=dt_timezone.utc),
        "views": 20,
        "sender": SimpleNamespace(id=42),
        "media": None,
        "action": None,
        "is_reply": False,
        "reply_to": None,
        "web_preview": None,
        "photo": None,
        "video": None,
        "audio": None,
        "voice": None,
        "gif": None,
        "sticker": None,
        "contact": None,
        "geo": None,
        "document": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeTelegramClient:
    messages = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def start(self):
        return self

    def iter_messages(self, **kwargs):
        return FakeAsyncIterator(self.messages)

    def iter_participants(self, **kwargs):
        return FakeAsyncIterator()


@pytest.mark.django_db
def test_extract_msgs_builds_message_and_advances_room_offset(room_factory):
    room = room_factory(access_hash=123)
    client = FakeTelegramClient()
    client.messages = [telegram_message()]

    with patch("app_telegram.services.telegram.detect_lang", return_value="en"):
        messages, updated_room, updated_parent = asyncio.run(
            extract_msgs(client, room, max_msgs=100)
        )

    assert updated_parent is None
    assert updated_room is room
    assert room.last_offset == 101
    assert room.last_offset_update is not None
    assert len(messages) == 1
    message = messages[0]
    assert message.link == "https://t.me/test_room/101"
    assert message.msg_id == 101
    assert message.sender == 42
    assert message.views == 20
    assert message.media_type == MessageItem.TEXT
    assert message.annotated_text == "Hello world"
    assert message.lang == "en"
    assert message.ner_input_hash


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("media_fields", "expected_type"),
    [
        ({"media": object(), "photo": object()}, MessageItem.PHOTO),
        ({"media": object(), "video": object()}, MessageItem.VIDEO),
        ({"media": object(), "voice": object()}, MessageItem.AUDIO),
        ({"media": object(), "document": object()}, MessageItem.DOC),
        ({"media": object(), "gif": object()}, MessageItem.GIF),
    ],
)
def test_extract_msgs_classifies_media(room_factory, media_fields, expected_type):
    room = room_factory(access_hash=123)
    client = FakeTelegramClient()
    client.messages = [telegram_message(**media_fields)]

    with patch("app_telegram.services.telegram.detect_lang", return_value="en"):
        messages, _, _ = asyncio.run(extract_msgs(client, room, max_msgs=100))

    assert messages[0].media_type == expected_type


@pytest.mark.django_db
def test_extract_msgs_ignores_telegram_action_messages(room_factory):
    room = room_factory(access_hash=123)
    client = FakeTelegramClient()
    client.messages = [telegram_message(action=object())]

    messages, updated_room, updated_parent = asyncio.run(
        extract_msgs(client, room, max_msgs=100)
    )

    assert messages == []
    # Telegram action records create no MessageItem, but the request itself
    # finished normally and must count as a completed room scan.
    assert updated_room is room
    assert updated_parent is None
    assert room.last_offset == 0
    assert room.last_scan_at is not None


@pytest.mark.django_db
def test_extract_msgs_builds_comment_link_and_updates_parent(room_factory, message_factory):
    room = room_factory(access_hash=123)
    parent = message_factory(room=room, msg_id=10, link="https://t.me/test_room/10")
    client = FakeTelegramClient()
    client.messages = [
        telegram_message(
            msg_id=201,
            text="A reply",
            is_reply=True,
            reply_to=SimpleNamespace(reply_to_msg_id=10),
        )
    ]

    with patch("app_telegram.services.telegram.detect_lang", return_value="en"):
        messages, updated_room, updated_parent = asyncio.run(
            extract_msgs(client, room, max_msgs=100, related_msg=parent)
        )

    assert updated_room is None
    assert updated_parent is parent
    assert parent.last_offset == 201
    assert messages[0].link == "https://t.me/test_room/10?comment=201"
    assert messages[0].reply_to_id == 10
    assert messages[0].reply_to_msg is parent


@pytest.mark.django_db
def test_extract_msgs_handles_story_reply_header(room_factory):
    room = room_factory(access_hash=123)
    client = FakeTelegramClient()
    client.messages = [
        telegram_message(
            msg_id=202,
            text="A story reply",
            is_reply=True,
            reply_to=MessageReplyStoryHeader(peer=PeerChannel(456), story_id=7),
        )
    ]

    with patch("app_telegram.services.telegram.detect_lang", return_value="en"):
        messages, updated_room, updated_parent = asyncio.run(
            extract_msgs(client, room, max_msgs=100)
        )

    assert updated_room is room
    assert updated_parent is None
    assert room.last_offset == 202
    assert messages[0].is_reply is True
    assert messages[0].reply_to_id is None
    assert messages[0].reply_to_msg is None


class FakePrivateError(Exception):
    pass


@pytest.mark.django_db
def test_extract_msgs_marks_private_room(room_factory):
    room = room_factory(access_hash=123)
    client = FakeTelegramClient()
    client.iter_messages = lambda **kwargs: FakeAsyncIterator(exception=FakePrivateError())

    with patch("app_telegram.services.telegram.ChannelPrivateError", FakePrivateError):
        messages, updated_room, updated_parent = asyncio.run(
            extract_msgs(client, room, max_msgs=100)
        )

    assert messages == []
    assert updated_room is room
    assert updated_parent is None
    assert room.is_private is True


@pytest.mark.django_db(transaction=True)
def test_scan_rooms_persists_messages_and_query_relation(
    room_factory,
    telegram_auth_factory,
    query,
):
    auth = telegram_auth_factory()
    room = room_factory(access_auth=auth, access_hash=123)
    client = type("SuccessfulScanClient", (FakeTelegramClient,), {})
    client.messages = [telegram_message(msg_id=301, text="Collected message")]
    auth_data = {
        "pk": auth.pk,
        "name": auth.name,
        "api_id": auth.api_id,
        "api_hash": auth.api_hash,
        "session": auth.session,
    }

    with (
        patch("app_telegram.services.telegram.TelegramClient", client),
        patch("app_telegram.services.telegram.StringSession", return_value="session"),
        patch("app_telegram.services.telegram.detect_lang", return_value="en"),
    ):
        message_pks = scan_rooms([room.pk], auth_data, max_msgs=100, token=query.pk)

    room.refresh_from_db()
    message = MessageItem.objects.get(pk=message_pks[0])
    assert message.text == "Collected message"
    assert message.room == room
    assert room.last_offset == 301
    assert QueryItem.objects.filter(query=query, object_id=message.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_scan_comments_persists_reply_and_updates_parent(
    room_factory,
    telegram_auth_factory,
    message_factory,
):
    auth = telegram_auth_factory()
    room = room_factory(access_auth=auth, access_hash=123, is_channel=True)
    parent = message_factory(
        room=room,
        msg_id=10,
        link="https://t.me/test_room/10",
        is_reply=False,
    )
    client = type("SuccessfulCommentClient", (FakeTelegramClient,), {})
    client.messages = [
        telegram_message(
            msg_id=401,
            text="Collected reply",
            is_reply=True,
            reply_to=SimpleNamespace(reply_to_msg_id=10),
        )
    ]
    auth_data = {
        "pk": auth.pk,
        "name": auth.name,
        "api_id": auth.api_id,
        "api_hash": auth.api_hash,
        "session": auth.session,
    }

    with (
        patch("app_telegram.services.telegram.TelegramClient", client),
        patch("app_telegram.services.telegram.StringSession", return_value="session"),
        patch("app_telegram.services.telegram.detect_lang", return_value="en"),
    ):
        message_pks = scan_comments(
            [parent.pk],
            auth_data,
            max_msgs=100,
        )

    parent.refresh_from_db()
    reply = MessageItem.objects.get(pk=message_pks[0])
    assert reply.reply_to_msg == parent
    assert reply.link == "https://t.me/test_room/10?comment=401"
    assert parent.last_offset == 401
    assert parent.last_scan_at is not None
