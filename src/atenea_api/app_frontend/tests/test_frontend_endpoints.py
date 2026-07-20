from unittest.mock import patch

import pytest
from rest_framework import status
from rest_framework_api_key.models import APIKey

from app_base.languages import LANGS_ISO_639_1


LANGUAGES_URL = "/api/v1/front/form/languages"
TAGS_URL = "/api/v1/front/form/tags"


def authenticate_with_api_key(api_client):
    _, key = APIKey.objects.create_key(name="frontend-tests")
    api_client.credentials(HTTP_FRONT_API_KEY=key)


@pytest.mark.django_db
@pytest.mark.parametrize("url", [LANGUAGES_URL, TAGS_URL])
def test_frontend_endpoints_require_api_key(api_client, url):
    response = api_client.get(url)

    assert response.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }


@pytest.mark.django_db
def test_languages_endpoint_returns_all_supported_codes(api_client):
    authenticate_with_api_key(api_client)

    response = api_client.get(LANGUAGES_URL)

    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == len(LANGS_ISO_639_1)
    assert response.data["results"] == [code for code, _ in LANGS_ISO_639_1]
    assert response.data["token"] is not None


@pytest.mark.django_db
def test_tags_endpoint_normalizes_sorts_and_deduplicates_tags(api_client, room_factory):
    room_factory(
        tg_id=31001,
        unique_name="front_one",
        link="https://t.me/front_one",
        tags=["News", "Research"],
    )
    room_factory(
        tg_id=31002,
        unique_name="front_two",
        link="https://t.me/front_two",
        tags=["news", "Data"],
    )
    authenticate_with_api_key(api_client)

    response = api_client.get(TAGS_URL)

    assert response.status_code == status.HTTP_200_OK
    assert response.data["results"] == ["data", "news", "research"]
    assert response.data["count"] == 3


@pytest.mark.django_db
def test_frontend_endpoint_returns_500_for_unexpected_failure(api_client):
    authenticate_with_api_key(api_client)

    with patch(
        "app_frontend.views.create_paginate_response",
        side_effect=RuntimeError("pagination failed"),
    ):
        response = api_client.get(LANGUAGES_URL)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.data["token"] is not None
