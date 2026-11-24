from datetime import date, datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.utils import timezone
from elasticsearch.dsl import Search
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from app_stats.services.api import (
    get_message_created_at_minimum,
    get_message_stats,
)
from app_stats.views import MessageStatsViewSet


def _bucket_key(value):
    return int(value.timestamp() * 1000)


def _search_patches(response, *, query=None):
    query = query or {"term": {"is_reply": False}}
    captured = {}

    def execute(search):
        captured.update(search.to_dict())
        return response

    return (
        patch("app_stats.services.api.connections.get_connection", return_value=object()),
        patch("app_telegram.services.analytics.build_message_filter", return_value=query),
        patch.object(Search, "execute", autospec=True, side_effect=execute),
        captured,
    )


def test_elasticsearch_histogram_uses_calendar_timezone_and_population_z_score():
    buckets = [
        {"key": _bucket_key(datetime(2023, 12, 31, 23, tzinfo=dt_timezone.utc)), "doc_count": 1, "z_score": {"value": -1.0}},
        {"key": _bucket_key(datetime(2024, 1, 1, 23, tzinfo=dt_timezone.utc)), "doc_count": 3, "z_score": {"value": 1.0}},
    ]
    response = SimpleNamespace(aggregations=SimpleNamespace(periods=SimpleNamespace(buckets=buckets)))
    connection_patch, filter_patch, execute_patch, captured = _search_patches(response)

    with timezone.override("Europe/Madrid"), connection_patch, filter_patch as build_filter, execute_patch:
        rows = get_message_stats(
            createdat_min=date(2024, 1, 1),
            createdat_max=date(2024, 1, 3),
            group_by="day",
            room=["TelegramRoom"],
            is_reply=False,
            stored_since=date(2023, 12, 1),
            tags=["Research"],
            tag_match="all",
            z_score=True,
        )

    assert rows[0]["period_start"] == datetime(2024, 1, 1, tzinfo=ZoneInfo("Europe/Madrid"))
    assert [row["count"] for row in rows] == [1, 3]
    assert [row["z_score"] for row in rows] == [-1.0, 1.0]
    build_filter.assert_called_once_with(
        createdat_min=date(2024, 1, 1),
        createdat_max=date(2024, 1, 3),
        room=["TelegramRoom"],
        tags=["Research"],
        tag_match="all",
        is_reply=False,
        stored_since=date(2023, 12, 1),
    )
    histogram = captured["aggs"]["periods"]["date_histogram"]
    assert histogram == {
        "field": "created_at",
        "calendar_interval": "day",
        "time_zone": "Europe/Madrid",
        "min_doc_count": 1,
    }
    normalize = captured["aggs"]["periods"]["aggs"]["z_score"]["normalize"]
    assert normalize == {"buckets_path": "_count", "method": "z-score"}
    assert captured["size"] == 0


def test_elasticsearch_histogram_converts_null_and_non_finite_scores_to_zero():
    for counts, scores in (((2,), (None,)), ((2, 2), (float("nan"), float("nan")))):
        buckets = [
            {
                "key": _bucket_key(datetime(2024, 1, day, tzinfo=dt_timezone.utc)),
                "doc_count": count,
                "z_score": {"value": score},
            }
            for day, (count, score) in enumerate(zip(counts, scores), start=1)
        ]
        response = SimpleNamespace(
            aggregations=SimpleNamespace(periods=SimpleNamespace(buckets=buckets))
        )
        connection_patch, filter_patch, execute_patch, _ = _search_patches(response)

        with connection_patch, filter_patch, execute_patch:
            rows = get_message_stats(z_score=True)

            assert [row["z_score"] for row in rows] == [0.0] * len(counts)


def test_undated_histogram_bucket_participates_in_population_and_is_returned_last():
    first = _bucket_key(datetime(2024, 1, 1, tzinfo=dt_timezone.utc))
    sentinel = first - 367 * 86400000
    buckets = [
        {"key": sentinel, "doc_count": 5, "z_score": {"value": 1.224744871391589}},
        {"key": first, "doc_count": 1, "z_score": {"value": -1.224744871391589}},
        {"key": _bucket_key(datetime(2025, 1, 1, tzinfo=dt_timezone.utc)),
         "doc_count": 3, "z_score": {"value": 0.0}},
    ]
    response = SimpleNamespace(aggregations=SimpleNamespace(periods=SimpleNamespace(buckets=buckets)))
    connection_patch, filter_patch, execute_patch, captured = _search_patches(response)
    with connection_patch, filter_patch, execute_patch, patch(
        "app_stats.services.api._undated_period", return_value=(5, sentinel, first),
    ):
        rows = get_message_stats(z_score=True)
    assert [row["count"] for row in rows] == [1, 3, 5]
    assert rows[-1]["period_start"] is None
    assert rows[-1]["z_score"] == 1.224744871391589
    assert captured["aggs"]["periods"]["date_histogram"]["missing"] == sentinel


def test_only_undated_messages_keep_singleton_zero_score_without_a_histogram():
    response = SimpleNamespace(aggregations=SimpleNamespace(
        undated={"doc_count": 5}, first_created_at=SimpleNamespace(value=None),
    ))
    connection_patch, filter_patch, execute_patch, captured = _search_patches(response)
    with connection_patch, filter_patch, execute_patch:
        rows = get_message_stats(z_score=True)
    assert rows == [{"period_start": None, "count": 5, "z_score": 0.0}]
    assert "periods" not in captured["aggs"]


def test_elasticsearch_minimum_uses_the_filtered_search_and_min_metric():
    minimum = datetime(2021, 5, 4, 8, 30, tzinfo=dt_timezone.utc)
    response = SimpleNamespace(
        aggregations=SimpleNamespace(first_created_at=SimpleNamespace(value=minimum.timestamp() * 1000))
    )
    connection_patch, filter_patch, execute_patch, captured = _search_patches(response)

    with connection_patch, filter_patch, execute_patch:
        value = get_message_created_at_minimum(
            createdat_min=date(2000, 1, 1),
            createdat_max=date(2024, 1, 1),
            room=["telegram"],
            tags=["ExactTag"],
        )

    assert value == minimum
    assert captured["aggs"]["first_created_at"] == {"min": {"field": "created_at"}}
    assert captured["size"] == 0






def test_statistics_endpoint_keeps_the_paginated_json_contract_with_elasticsearch_rows():
    rows = [
        {"period_start": datetime(2024, 1, day, tzinfo=dt_timezone.utc), "count": day}
        for day in (1, 2, 3)
    ]
    request = APIRequestFactory().get("/api/v1/stats/msg", {"page-size": 2})
    force_authenticate(
        request,
        user=SimpleNamespace(pk="staff", is_authenticated=True, is_staff=True),
    )
    view = MessageStatsViewSet.as_view({"get": "list"})

    with patch("app_stats.views.parse_request_data", return_value=({"group_by": "day"}, None)), \
            patch("app_stats.views.get_message_stats", return_value=rows):
        response = view(request)

    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == 3
    assert response.data["token"] is None
    assert response.data["next"] is not None
    assert response.data["previous"] is None
    assert [row["count"] for row in response.data["results"]] == [1, 2]
    assert response.data["results"][0]["period_start"] == "2024-01-01T00:00:00Z"
