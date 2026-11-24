from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

import pytest
from rest_framework import status


STATS_URL = "/api/v1/stats/msg"


@pytest.mark.django_db
def test_statistics_endpoint_requires_authentication(api_client):
    response = api_client.get(STATS_URL)

    assert response.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("is_staff", [False, True])
def test_statistics_endpoint_returns_filtered_results(api_client, staff_user, is_staff):
    staff_user.is_staff = is_staff
    staff_user.save(update_fields=["is_staff"])
    api_client.force_authenticate(user=staff_user)
    rows = [{
        "period_start": datetime(2024, 5, 1, tzinfo=dt_timezone.utc),
        "count": 1,
    }]

    with patch("app_stats.views.get_message_stats", return_value=rows) as stats:
        response = api_client.get(
            STATS_URL,
            {"room": ["endpoint_stats"], "tag": ["paper"], "group-by": "day"},
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == 1
    assert response.data["results"][0]["count"] == 1
    stats.assert_called_once()


@pytest.mark.django_db
def test_statistics_endpoint_rejects_invalid_grouping(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(STATS_URL, {"group-by": "hour"})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "errors" in response.data
