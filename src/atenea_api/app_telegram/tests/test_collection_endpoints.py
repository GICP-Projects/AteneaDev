from unittest.mock import patch

import pytest
from rest_framework import status

from app_base.models import Query
from app_users.models import User


ROOM_SCAN_URL = "/api/v1/tg/room/scan"
ROOM_ACCESS_URL = "/api/v1/tg/room/access"
MESSAGE_SCAN_URL = "/api/v1/tg/msg/scan"


@pytest.mark.django_db
@pytest.mark.parametrize("url", [ROOM_SCAN_URL, ROOM_ACCESS_URL, MESSAGE_SCAN_URL])
def test_collection_endpoints_require_staff(api_client, url):
    response = api_client.get(url)

    assert response.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("url", [ROOM_SCAN_URL, ROOM_ACCESS_URL, MESSAGE_SCAN_URL])
def test_collection_endpoints_reject_authenticated_non_staff(api_client, url):
    user = User.objects.create_user(
        email="reader@example.org",
        username="reader",
        password="test-password",
    )
    api_client.force_authenticate(user=user)

    response = api_client.get(url)

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_room_scan_validates_and_forwards_parameters(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    with patch(
        "app_telegram.views.staff_views.api.room_scanning",
        return_value=2,
    ) as room_scanning:
        response = api_client.get(
            ROOM_SCAN_URL,
            {
                "room": ["room_a", "room_b"],
                "tag": ["research", "news"],
                "lang": ["es"],
                "max-msgs": 100,
                "update-users": "true",
            },
        )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert "2 channels/groups" in response.data["message"]
    assert Query.objects.filter(token=response.data["token"]).exists()
    room_scanning.assert_called_once()
    token = room_scanning.call_args.args[0]
    params = room_scanning.call_args.kwargs
    assert str(token) == response.data["token"]
    assert params == {
        "room": ["room_a", "room_b"],
        "tags": ["research", "news"],
        "lang": ["es"],
        "max_msgs": 100,
        "update_users": True,
        "tag_match": "any",
    }


@pytest.mark.django_db
def test_room_access_validates_and_forwards_parameters(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    with patch(
        "app_telegram.views.staff_views.api.room_access",
        return_value=3,
    ) as room_access:
        response = api_client.get(
            ROOM_ACCESS_URL,
            {
                "room": ["orphan_a"],
                "tag": ["recover"],
                "lang": ["es"],
                "is-channel": "true",
            },
        )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert "3 channels/groups" in response.data["message"]
    room_access.assert_called_once()
    assert room_access.call_args.kwargs["room"] == ["orphan_a"]
    assert room_access.call_args.kwargs["tags"] == ["recover"]
    assert room_access.call_args.kwargs["lang"] == ["es"]
    assert room_access.call_args.kwargs["is_channel"] is True


@pytest.mark.django_db
def test_message_scan_validates_and_forwards_parameters(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    with patch(
        "app_telegram.views.staff_views.api.msg_scanning_comments",
        return_value=1,
    ) as scan_comments:
        response = api_client.get(
            MESSAGE_SCAN_URL,
            {
                "room": ["channel_a"],
                "tag": ["paper"],
                "lang": ["en"],
                "type": ["ONLY_TEXT"],
                "max-msgs": 25,
                "total-msgs": 10000,
            },
        )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert "1 channels/groups" in response.data["message"]
    scan_comments.assert_called_once()
    assert scan_comments.call_args.kwargs["room"] == ["channel_a"]
    assert scan_comments.call_args.kwargs["tags"] == ["paper"]
    assert scan_comments.call_args.kwargs["lang"] == ["en"]
    assert scan_comments.call_args.kwargs["type"] == ["ONLY_TEXT"]
    assert scan_comments.call_args.kwargs["max_msgs"] == 25
    assert scan_comments.call_args.kwargs["total_msgs"] == 10000


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url", "params"),
    [
        (ROOM_SCAN_URL, {"max-msgs": 9}),
        (ROOM_SCAN_URL, {"max-msgs": 50001}),
        (MESSAGE_SCAN_URL, {"max-msgs": 9}),
        (MESSAGE_SCAN_URL, {"total-msgs": 9999}),
    ],
)
def test_collection_endpoints_reject_invalid_limits(api_client, staff_user, url, params):
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(url, params)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "errors" in response.data


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url", "target"),
    [
        (ROOM_SCAN_URL, "app_telegram.views.staff_views.api.room_scanning"),
        (ROOM_ACCESS_URL, "app_telegram.views.staff_views.api.room_access"),
        (MESSAGE_SCAN_URL, "app_telegram.views.staff_views.api.msg_scanning_comments"),
    ],
)
def test_collection_endpoints_return_500_for_unexpected_service_failure(
    api_client,
    staff_user,
    url,
    target,
):
    api_client.force_authenticate(user=staff_user)

    with patch(target, side_effect=RuntimeError("scheduler unavailable")):
        response = api_client.get(url)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert Query.objects.filter(token=response.data["token"]).exists()
