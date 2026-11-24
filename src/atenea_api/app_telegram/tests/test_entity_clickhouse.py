"""Transport, parameter binding and cursor coverage for entity rankings."""
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import pytest
from rest_framework import pagination
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from app_telegram.pagination import EntityRankingPaginator
from app_telegram.services import analytics
from app_telegram.services.index import _write_actions


def test_message_bulk_does_not_ignore_missing_index_errors():
    with patch("elasticsearch.helpers.bulk", return_value=(0, [{
        "index": {"status": 404, "error": {"type": "index_not_found_exception"}},
    }])):
        with pytest.raises(RuntimeError, match="Bulk indexing failed"):
            _write_actions(MagicMock(), [{"_op_type": "index", "_id": "message"}])


def test_message_bulk_accepts_already_deleted_documents():
    with patch("elasticsearch.helpers.bulk", return_value=(0, [{
        "delete": {"status": 404, "result": "not_found"},
    }])):
        _write_actions(MagicMock(), [{"_op_type": "delete", "_id": "message"}])


def test_signed_aggregates_and_parameterized_cursor():
    entity = "00000000-0000-0000-0000-000000000009"
    query, params = analytics.build_entity_ranking_query(
        {"createdat_min": "2025-01-01", "is_reply": False},
        position=[9, 15, entity], limit=21,
    )
    assert "sum(toInt64(sign)) AS message_count" in query
    assert "sum(toInt64(sign) * count) AS appearance_count" in query
    assert "message_count > 0" in query
    assert "message_count < {cursor0:Int64}" in query
    assert "toString(entity_id) > {cursor2:String}" in query
    assert "2025-01-01" not in query
    assert params == {"limit": 21, "createdat_min": "2025-01-01", "is_reply": False,
                      "cursor0": 9, "cursor1": 15, "cursor2": entity}


def test_reverse_cursor_and_order():
    query, params = analytics.build_entity_ranking_query(
        {}, position=[4, 8, "00000000-0000-0000-0000-000000000004"], reverse=True,
    )
    assert "message_count > {cursor0:Int64}" in query
    assert "toString(entity_id) < {cursor2:String}" in query
    assert "ORDER BY message_count ASC, appearance_count ASC, toString(entity_id) DESC" in query


def test_large_counts_and_uuid_are_preserved():
    client = MagicMock()
    entity_id = UUID("00000000-0000-0000-0000-000000000001")
    client.query.return_value.named_results.return_value = iter([{
        "entity_id": str(entity_id), "message_count": 3_000_000_001,
        "appearance_count": 4_000_000_002,
    }])
    assert analytics.entity_ranking({}, client=client) == [{
        "entity_id": entity_id, "message_count": 3_000_000_001,
        "appearance_count": 4_000_000_002,
    }]
    client.close.assert_not_called()


@pytest.mark.parametrize("ordering", [("entity_id",), ("-count",), ("DROP TABLE",)])
def test_invalid_order_is_rejected(ordering):
    with pytest.raises(ValueError):
        analytics.build_entity_ranking_query({}, ordering=ordering)


def test_entity_paginator_reverses_previous_page_and_keeps_exact_ties(monkeypatch):
    factory = APIRequestFactory()
    original_request = Request(factory.get("/entities/?order=messages"))
    paginator = EntityRankingPaginator(page_size=2)
    paginator.request = original_request
    paginator.base_url = original_request.build_absolute_uri()
    paginator.ordering = ("-message_count", "-appearance_count", "entity_id")
    paginator.ordering_fields = paginator._get_ordering_fields(None)
    cursor_url = paginator.encode_cursor(pagination.Cursor(
        offset=0,
        reverse=True,
        position='["8", "8", "00000000-0000-0000-0000-000000000008"]',
    ))
    token = parse_qs(urlsplit(cursor_url).query)["cursor"][0]
    previous_request = Request(factory.get(
        "/entities/?" + urlencode({"order": "messages", "cursor": token}),
    ))

    candidates = [
        {"message_count": 7, "appearance_count": 7, "entity_id": "00000000-0000-0000-0000-000000000007"},
        {"message_count": 6, "appearance_count": 6, "entity_id": "00000000-0000-0000-0000-000000000006"},
        {"message_count": 5, "appearance_count": 5, "entity_id": "00000000-0000-0000-0000-000000000005"},
    ]
    mocked_ranking = MagicMock(side_effect=[
        candidates,
        [{"message_count": 8, "appearance_count": 8, "entity_id": "00000000-0000-0000-0000-000000000008"}],
    ])
    monkeypatch.setattr(analytics, "entity_ranking", mocked_ranking)

    page = paginator.paginate_ranking(
        {}, previous_request, ordering=paginator.ordering,
    )

    assert [row["entity_id"] for row in page] == [
        "00000000-0000-0000-0000-000000000006",
        "00000000-0000-0000-0000-000000000007",
    ]
    assert paginator.has_previous is True
    assert paginator.has_next is True
    assert parse_qs(urlsplit(paginator.get_previous_link()).query)["cursor"]
    assert parse_qs(urlsplit(paginator.get_next_link()).query)["cursor"]
    assert mocked_ranking.call_args_list[1].kwargs["position"] == (
        '["7","7","00000000-0000-0000-0000-000000000007"]'
    )


def test_entity_paginator_uses_constructor_ordering_and_skips_first_page_probe(monkeypatch):
    ordering = ("-appearance_count", "-message_count", "entity_id")
    request = Request(APIRequestFactory().get("/entities/?order=appearances"))
    paginator = EntityRankingPaginator(page_size=2, ordering=ordering)
    candidates = [
        {"message_count": 1, "appearance_count": 9, "entity_id": "00000000-0000-0000-0000-000000000001"},
        {"message_count": 2, "appearance_count": 8, "entity_id": "00000000-0000-0000-0000-000000000002"},
        {"message_count": 1, "appearance_count": 7, "entity_id": "00000000-0000-0000-0000-000000000003"},
    ]
    mocked_ranking = MagicMock(return_value=candidates)
    monkeypatch.setattr(analytics, "entity_ranking", mocked_ranking)

    page = paginator.paginate_ranking({}, request)

    assert page == candidates[:2]
    assert paginator.ordering == ordering
    assert mocked_ranking.call_count == 1
    assert mocked_ranking.call_args.kwargs["ordering"] == ordering
    assert paginator.has_next is True
    assert paginator.has_previous is False
