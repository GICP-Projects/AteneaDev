from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from app_metadata.models import EmbeddingsItem
from app_metadata.services.embeddings import (
    _validate_openai_base_url,
    calculate_embeddings,
    clean_text_to_embed,
    cosine_similarity_many_to_many,
)


def test_validate_openai_base_url_requires_version_path():
    assert _validate_openai_base_url("http://provider:8000/v1/") == "http://provider:8000/v1"
    with pytest.raises(ValueError):
        _validate_openai_base_url("http://provider:8000")


def test_clean_text_to_embed_removes_external_noise():
    cleaned = clean_text_to_embed("Hello @alice https://example.org test@example.org")
    assert "@alice" not in cleaned
    assert "https://" not in cleaned
    assert "test@example.org" not in cleaned


def test_cosine_similarity_preserves_matrix_shape_and_normalizes_rows():
    similarities = cosine_similarity_many_to_many(
        np.array([[1.0, 0.0], [0.0, 1.0]]),
        np.array([[1.0, 0.0], [0.0, 1.0]]),
    )
    assert similarities.shape == (2, 2)
    assert similarities.tolist() == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.django_db(transaction=True)
def test_calculate_embeddings_persists_success_and_partial_failure(
    message_factory,
    room_factory,
):
    room = room_factory(unique_name="embedding_room", tg_id=30001)
    first = message_factory(
        room=room,
        msg_id=1,
        link="https://t.me/embedding_room/1",
        text="First text to embed",
    )
    second = message_factory(
        room=room,
        msg_id=2,
        link="https://t.me/embedding_room/2",
        text="Second text whose provider fails",
    )

    with (
        patch(
            "app_metadata.services.embeddings.call_embed_service",
            new=AsyncMock(return_value=("embed-model", "N/A", [[0.1, 0.2], None])),
        ),
        patch("app_metadata.services.embeddings.upsert_points") as upsert_points,
    ):
        calculate_embeddings(
            [first.pk, second.pk],
            "MessageItem",
            "app_telegram",
            "text",
            "embeddings",
            chunk_size=2,
            thread_count=1,
        )

    first.refresh_from_db()
    second.refresh_from_db()
    embeddings = EmbeddingsItem.objects.filter(
        pk__in=[first.embeddings_id, second.embeddings_id]
    )
    synced = embeddings.get(sync_status=EmbeddingsItem.STATUS_SYNCED)
    failed = embeddings.get(sync_status=EmbeddingsItem.STATUS_FAILED)
    assert synced.vector_dim == 2
    assert "provider" in failed.sync_error.lower()
    upsert_points.assert_called_once()
    assert len(upsert_points.call_args.kwargs["points"]) == 1
    payload = upsert_points.call_args.kwargs["points"][0]["payload"]
    assert payload["room_id"] == str(room.pk)
    assert payload["room_unique_name"] == room.unique_name


@pytest.mark.django_db(transaction=True)
def test_embedding_writer_reloads_room_after_payload_was_prepared(message_factory, monkeypatch):
    from django.db import transaction
    from django.utils import timezone
    from app_metadata.services import embeddings
    from app_telegram.services.room_reconciliation import RoomIdentitySnapshot, reconcile_room

    message = message_factory(text="An embedding generated before a room rename")
    room = message.room
    build = embeddings._build_embedding_payload

    def build_then_rename(*args, **kwargs):
        payload = build(*args, **kwargs)
        assert payload["room_unique_name"] == room.unique_name
        with transaction.atomic():
            reconcile_room(room, RoomIdentitySnapshot(
                room.tg_id, "renamed", room.title, timezone.now(),
                about=room.about, about_observed=True,
            ))
        return payload

    monkeypatch.setattr(embeddings, "_build_embedding_payload", build_then_rename)
    with patch.object(embeddings, "call_embed_service", AsyncMock(return_value=("model", "1", [[0.1, 0.2]]))), patch.object(embeddings, "upsert_points") as write:
        calculate_embeddings([message.pk], "MessageItem", "app_telegram", "text", "embeddings")
    payload = write.call_args.kwargs["points"][0]["payload"]
    assert payload["room_unique_name"] == "renamed"
    assert payload["room_id"] == str(room.pk)


@pytest.mark.parametrize("status", ["completed", "acknowledged", "wait_timeout"])
def test_vector_upsert_requires_applied_response_and_ordered_writes(status):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app_metadata.vector_store import _upsert_batch_with_retry

    client = Mock()
    client.upsert.return_value = SimpleNamespace(status=status)
    models = SimpleNamespace(PointStruct=lambda **kwargs: kwargs)
    points = [{"id": "point", "vector": [0.1], "payload": {"room_id": "room"}}]
    succeeded, failed = _upsert_batch_with_retry(
        client, models, "collection", points, max_retries=0, retry_delay_seconds=0,
    )
    assert succeeded == (points if status == "completed" else [])
    assert failed == ([] if status == "completed" else points)
    assert client.upsert.call_args.kwargs["ordering"] == "strong"
