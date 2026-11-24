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
    response = api_client.post(url, {}, format="json")

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

    response = api_client.post(url, {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_room_scan_validates_and_forwards_parameters(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    with patch(
        "app_telegram.views.staff_views.api.room_scanning",
        return_value=2,
    ) as room_scanning:
        response = api_client.post(
            ROOM_SCAN_URL,
            {
                "room": ["room_a", "room_b"],
                "tag": ["research", "news"],
                "lang": ["es"],
                "max-msgs": 100,
                "update-users": "true",
            },
            format="json",
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
        "check_room": False,
        "tag_match": "any",
        "is_channel": None,
    }


@pytest.mark.django_db
def test_room_access_validates_and_forwards_parameters(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    with patch(
        "app_telegram.views.staff_views.api.room_access",
        return_value=3,
    ) as room_access:
        response = api_client.post(
            ROOM_ACCESS_URL,
            {
                "room": ["orphan_a"],
                "tag": ["recover"],
                "lang": ["es"],
                "is-channel": "true",
            },
            format="json",
        )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert "3 channels/groups" in response.data["message"]
    room_access.assert_called_once()
    assert room_access.call_args.kwargs["room"] == ["orphan_a"]
    assert room_access.call_args.kwargs["tags"] == ["recover"]
    assert room_access.call_args.kwargs["lang"] == ["es"]
    assert room_access.call_args.kwargs["is_channel"] is True


@pytest.mark.django_db
def test_room_access_response_exposes_processing_job_id(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    with patch(
        "app_telegram.views.staff_views.api.room_access",
        return_value={"items_count": 3, "job_id": "durable-access-job"},
    ):
        response = api_client.post(ROOM_ACCESS_URL, {}, format="json")

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.data["results"] == {"job_id": "durable-access-job"}
    assert "3 channels/groups" in response.data["message"]


@pytest.mark.django_db
def test_message_scan_validates_and_forwards_parameters(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    with patch(
        "app_telegram.views.staff_views.api.msg_scanning_comments",
        return_value={"items_count": 1, "rooms_count": 1, "job_id": "comments-job", "message_job_id": "messages-job"},
    ) as scan_comments:
        response = api_client.post(
            MESSAGE_SCAN_URL,
            {
                "room": ["channel_a"],
                "tag": ["paper"],
                "lang": ["en"],
                "type": ["ONLY_TEXT"],
                "max-msgs": 25,
            },
            format="json",
        )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert "1 parent messages in 1 channels" in response.data["message"]
    assert response.data["results"]["items_count"] == 1
    assert response.data["results"]["rooms_count"] == 1
    scan_comments.assert_called_once()
    assert scan_comments.call_args.kwargs["room"] == ["channel_a"]
    assert scan_comments.call_args.kwargs["tags"] == ["paper"]
    assert scan_comments.call_args.kwargs["lang"] == ["en"]
    assert scan_comments.call_args.kwargs["type"] == ["ONLY_TEXT"]
    assert scan_comments.call_args.kwargs["max_msgs"] == 25
    assert "total_msgs" not in scan_comments.call_args.kwargs


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url", "params"),
    [
        (ROOM_SCAN_URL, {"max-msgs": 9}),
        (ROOM_SCAN_URL, {"max-msgs": 50001}),
        (MESSAGE_SCAN_URL, {"max-msgs": 9}),
        (MESSAGE_SCAN_URL, {"max-msgs": 5001}),
    ],
)
def test_collection_endpoints_reject_invalid_limits(api_client, staff_user, url, params):
    api_client.force_authenticate(user=staff_user)

    response = api_client.post(url, params, format="json")

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
        response = api_client.post(url, {}, format="json")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert Query.objects.filter(token=response.data["token"]).exists()
