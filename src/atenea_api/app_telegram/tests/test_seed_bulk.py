from types import SimpleNamespace

import pytest
from rest_framework import status

from app_base.models import QueryItem
from app_telegram.models import RoomItem, SeedItem, UserItem
from app_telegram.serializers import MessageDocumentSerializer, SeedItemSerializer
from app_telegram.utils import telegram_link_normalizer


SEED_BULK_URL = "/api/v1/tg/seed/bulk"


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://t.me/Example_Channel", "https://t.me/example_channel"),
        ("http://t.me/example_channel?ref=test", "https://t.me/example_channel"),
        ("t.me/example_channel/123", "https://t.me/example_channel"),
        ("https://www.t.me/example_channel", "https://t.me/example_channel"),
        ("https://t.me/Joinchat_News/123", "https://t.me/joinchat_news"),
        (
            "https://t.me/JOINchat/Dm-Vwk9bRZb6m3sqqZlQfg/12",
            "https://t.me/joinchat/Dm-Vwk9bRZb6m3sqqZlQfg",
        ),
    ],
)
def test_telegram_link_normalizer_accepts_supported_variants(link, expected):
    assert telegram_link_normalizer(link, rebuild_link=True) == expected
    assert telegram_link_normalizer(link) == expected.removeprefix("https://t.me/")


@pytest.mark.parametrize(
    "link",
    [
        "",
        None,
        123,
        "https://example.org/channel",
        "https://not-t.me/channel",
        "https://example.org/t.me/channel",
        "https://t.me",
        "ftp://t.me/channel",
        "https://t.me/canal_ñ",
        "https://t.me/joinchat",
        "https://t.me/joinchat/!invalid",
    ],
)
def test_telegram_link_normalizer_rejects_non_telegram_urls(link):
    assert telegram_link_normalizer(link, rebuild_link=True) is None


@pytest.mark.django_db
def test_entities_and_serializers_share_public_link_normalization():
    serializer = SeedItemSerializer(
        data=[
            {"link": "HTTPS://T.ME/Valid_Channel", "type": "CHANNEL", "lang": "en"},
            {"link": "https://example.org/not-telegram", "type": "CHANNEL"},
        ],
        many=True,
        min_length=1,
        max_length=1000,
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["items"][0]["link"] == "https://t.me/valid_channel"
    assert "https://example.org/not-telegram" in serializer.validated_data["invalid_items"]
    assert RoomItem.normalize_username("@Valid_Channel") == "valid_channel"
    assert UserItem.build_public_link("@Valid_Channel") == "https://t.me/valid_channel"
    hit = SimpleNamespace(room_name="Valid_Channel", msg_id=123)
    assert MessageDocumentSerializer().get_link(hit) == "https://t.me/valid_channel/123"


@pytest.mark.django_db
def test_seed_bulk_serializer_rejects_all_normalized_duplicates():
    serializer = SeedItemSerializer(
        data=[
            {"link": "https://t.me/Duplicate", "type": "CHANNEL"},
            {"link": "http://t.me/duplicate?source=other", "type": "CHANNEL"},
        ],
        many=True,
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["items"] == []
    assert len(serializer.validated_data["invalid_items"]) == 2


@pytest.mark.django_db
def test_seed_bulk_serializer_rejects_existing_seed(seed_factory):
    seed_factory(link="https://t.me/already_exists")
    serializer = SeedItemSerializer(
        data=[{"link": "http://t.me/ALREADY_EXISTS", "type": "CHANNEL"}],
        many=True,
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["items"] == []
    assert "http://t.me/ALREADY_EXISTS" in serializer.validated_data["invalid_items"]


@pytest.mark.django_db
def test_seed_bulk_endpoint_requires_staff(api_client):
    response = api_client.post(
        SEED_BULK_URL,
        [{"link": "https://t.me/channel", "type": "CHANNEL"}],
        format="json",
    )
    assert response.status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN}


@pytest.mark.django_db
def test_seed_bulk_endpoint_best_effort(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)
    response = api_client.post(
        SEED_BULK_URL,
        [
            {"link": "https://t.me/Valid_Channel", "type": "CHANNEL", "lang": "en"},
            {"link": "https://example.org/not-telegram", "type": "CHANNEL"},
        ],
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert SeedItem.objects.filter(link="https://t.me/valid_channel").exists()
    assert len(response.data["results"]) == 1
    assert "https://example.org/not-telegram" in response.data["errors"]
    created_seed = SeedItem.objects.get(link="https://t.me/valid_channel")
    assert QueryItem.objects.filter(object_id=created_seed.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("payload", [[], {}, "not-a-list"])
def test_seed_bulk_endpoint_rejects_invalid_container(api_client, staff_user, payload):
    api_client.force_authenticate(user=staff_user)
    response = api_client.post(SEED_BULK_URL, payload, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_seed_bulk_endpoint_rejects_more_than_one_thousand_items(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)
    payload = [
        {"link": f"https://t.me/channel_{index}", "type": "CHANNEL"}
        for index in range(1001)
    ]
    response = api_client.post(SEED_BULK_URL, payload, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
