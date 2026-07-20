from datetime import datetime, timezone as dt_timezone

import pytest
from rest_framework import status

from app_stats.services.api import get_message_stats


STATS_URL = "/api/v1/stats/msg"


@pytest.mark.django_db
def test_message_stats_groups_counts_by_day(message_factory, room_factory):
    room = room_factory(tg_id=32001, unique_name="stats", link="https://t.me/stats")
    message_factory(
        room=room,
        msg_id=1,
        link="https://t.me/stats/1",
        created_at=datetime(2024, 1, 1, 10, tzinfo=dt_timezone.utc),
    )
    message_factory(
        room=room,
        msg_id=2,
        link="https://t.me/stats/2",
        created_at=datetime(2024, 1, 1, 12, tzinfo=dt_timezone.utc),
    )
    message_factory(
        room=room,
        msg_id=3,
        link="https://t.me/stats/3",
        created_at=datetime(2024, 1, 2, 9, tzinfo=dt_timezone.utc),
    )

    results = list(get_message_stats(group_by="day"))

    assert [result["count"] for result in results] == [2, 1]
    assert [result["period_start"].day for result in results] == [1, 2]


@pytest.mark.django_db
def test_message_stats_filters_room_tags_and_replies(message_factory, room_factory):
    selected = room_factory(
        tg_id=32101,
        unique_name="selected_stats",
        link="https://t.me/selected_stats",
        tags=["research"],
    )
    ignored = room_factory(
        tg_id=32102,
        unique_name="ignored_stats",
        link="https://t.me/ignored_stats",
        tags=["other"],
    )
    created_at = datetime(2024, 2, 1, 10, tzinfo=dt_timezone.utc)
    message_factory(
        room=selected,
        msg_id=1,
        link="https://t.me/selected_stats/1",
        created_at=created_at,
        is_reply=False,
    )
    message_factory(
        room=selected,
        msg_id=2,
        link="https://t.me/selected_stats/2",
        created_at=created_at,
        is_reply=True,
    )
    message_factory(
        room=ignored,
        msg_id=1,
        link="https://t.me/ignored_stats/1",
        created_at=created_at,
        is_reply=False,
    )

    results = list(
        get_message_stats(
            room=["selected_stats"],
            tags=["research"],
            is_reply=False,
        )
    )

    assert len(results) == 1
    assert results[0]["count"] == 1


@pytest.mark.django_db
def test_message_stats_calculates_z_scores(message_factory, room_factory):
    room = room_factory(tg_id=32201, unique_name="zscore", link="https://t.me/zscore")
    for index in range(1, 5):
        day = 1 if index == 1 else 2
        message_factory(
            room=room,
            msg_id=index,
            link=f"https://t.me/zscore/{index}",
            created_at=datetime(2024, 3, day, index, tzinfo=dt_timezone.utc),
        )

    results = get_message_stats(group_by="day", z_score=True)

    assert [result["count"] for result in results] == [1, 3]
    assert [result["z_score"] for result in results] == [-1.0, 1.0]


@pytest.mark.django_db
def test_message_stats_uses_zero_z_score_for_single_period(message_factory):
    message_factory(created_at=datetime(2024, 4, 1, tzinfo=dt_timezone.utc))

    results = get_message_stats(group_by="day", z_score=True)

    assert results[0]["z_score"] == 0.0


@pytest.mark.django_db
def test_statistics_endpoint_requires_staff(api_client):
    response = api_client.get(STATS_URL)

    assert response.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }


@pytest.mark.django_db
def test_statistics_endpoint_returns_filtered_results(
    api_client,
    staff_user,
    message_factory,
    room_factory,
):
    room = room_factory(
        tg_id=32301,
        unique_name="endpoint_stats",
        link="https://t.me/endpoint_stats",
        tags=["paper"],
    )
    message_factory(
        room=room,
        created_at=datetime(2024, 5, 1, tzinfo=dt_timezone.utc),
    )
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(
        STATS_URL,
        {"room": ["endpoint_stats"], "tag": ["paper"], "group-by": "day"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == 1
    assert response.data["results"][0]["count"] == 1


@pytest.mark.django_db
def test_statistics_endpoint_rejects_invalid_grouping(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(STATS_URL, {"group-by": "hour"})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "errors" in response.data

