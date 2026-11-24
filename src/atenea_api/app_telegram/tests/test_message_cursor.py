"""PostgreSQL coverage for signed message cursors and independent totals."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from uuid import UUID

import pytest
from django.core import signing
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import include, path
from drf_spectacular.generators import SchemaGenerator
from rest_framework import serializers, viewsets
from rest_framework.request import Request
from rest_framework.routers import SimpleRouter
from rest_framework.test import APIRequestFactory

from app_base.mixins import CountMixin
from app_base.models import Query
from app_base.views import CursorPaginator, create_paginate_response
from app_metadata.models import EmbeddingsItem
from app_telegram.models import MessageItem
from app_telegram.views.staff_views import MessageView

pytestmark = pytest.mark.django_db

MESSAGES = "/api/v1/tg/msg"
VECTORS = MESSAGES + "/vector"
DATE = datetime(2025, 10, 13, tzinfo=timezone.utc)


@pytest.fixture
def client(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)
    return api_client


def make_messages(room_factory, dates, vectors=False):
    room = room_factory(tags=["research", "news"])
    embeddings = []
    if vectors:
        embeddings = EmbeddingsItem.objects.bulk_create(
            [EmbeddingsItem(model="test", version="1") for _ in dates]
        )
    return MessageItem.objects.bulk_create([
        MessageItem(
            id=UUID(int=index + 1),
            room=room,
            msg_id=index + 1,
            link=f"https://t.me/{room.unique_name}/{index + 1}",
            media_type=MessageItem.TEXT,
            text=f"Message {index + 1}",
            created_at=date,
            embeddings=embeddings[index] if vectors else None,
        )
        for index, date in enumerate(dates)
    ])


def ids(response):
    return [item["link"] for item in response.data["results"]]


def replace_query(url, **changes):
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query.update({key: [str(value)] for key, value in changes.items()})
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


@pytest.mark.parametrize("url,vectors", [(MESSAGES, False), (VECTORS, True)])
@override_settings(REST_FRAMEWORK={"MAX_PAGES_AUTHENTICATED": 1})
def test_cursor_traverses_large_ties_and_nulls_in_both_directions(client, room_factory, url, vectors):
    dates = [DATE] * 1011 + [DATE + timedelta(days=1)] * 3 + [None] * 107
    items = make_messages(room_factory, dates, vectors)
    expected = [item.link for item in items]
    pages = []
    links = []
    link = url + "?page-size=100&tag=research"
    with CaptureQueriesContext(connection) as queries:
        while link:
            response = client.get(link)
            assert response.status_code == 200, response.data
            assert set(response.data) == {"next", "previous", "token", "results"}
            assert response.data["token"] is not None
            pages.append(ids(response))
            links.append(response.data["previous"])
            link = response.data["next"]
    assert [item for page in pages for item in page] == expected
    assert links[0] is None
    assert not any("COUNT(" in query["sql"].upper() or " OFFSET " in query["sql"].upper() for query in queries)
    message_queries = [
        query["sql"].upper() for query in queries
        if "APP_TELEGRAM_MESSAGEITEM" in query["sql"].upper()
    ]
    assert any("ROW(" in sql for sql in message_queries)
    assert any('"CREATED_AT" IS NULL' in sql for sql in message_queries)
    assert any('"CREATED_AT" IS NOT NULL' in sql for sql in message_queries)
    with CaptureQueriesContext(connection) as reverse_queries:
        response = client.get(links[-1])
        for index in range(len(pages) - 2, -1, -1):
            assert ids(response) == pages[index]
            forward = client.get(response.data["next"])
            assert forward.status_code == 200
            assert ids(forward) == pages[index + 1]
            if response.data["previous"]:
                response = client.get(response.data["previous"])
    assert response.data["previous"] is None
    assert not any(
        "COUNT(" in query["sql"].upper() or " OFFSET " in query["sql"].upper()
        for query in reverse_queries
    )


@pytest.mark.parametrize("url", [MESSAGES, VECTORS])
def test_empty_cursor_selection(client, url):
    response = client.get(url)
    assert response.status_code == 200
    assert response.data["results"] == []
    assert response.data["next"] is None
    assert response.data["previous"] is None
    assert "count" not in response.data


def test_cursor_survives_deleted_anchor_and_insertions(client, room_factory):
    items = make_messages(room_factory, [DATE + timedelta(days=index) for index in range(5)])
    response = client.get(MESSAGES + "?page-size=2")
    link = response.data["next"]
    with override_settings(ELASTICSEARCH_DSL_AUTOSYNC=False):
        items[1].delete()
    MessageItem.objects.create(
        id=UUID(int=100), room=items[0].room, msg_id=100, link="https://t.me/test_room/100",
        media_type=MessageItem.TEXT, created_at=DATE - timedelta(days=1),
    )
    MessageItem.objects.create(
        id=UUID(int=101), room=items[0].room, msg_id=101, link="https://t.me/test_room/101",
        media_type=MessageItem.TEXT, created_at=DATE + timedelta(days=10),
    )
    found = []
    while link:
        response = client.get(link)
        assert response.status_code == 200
        found.extend(ids(response))
        link = response.data["next"]
    assert found == [item.link for item in items[2:]] + ["https://t.me/test_room/101"]


@pytest.mark.parametrize("url,default_size", [(MESSAGES, 100), (VECTORS, 10)])
def test_page_size_defaults_and_maximum(client, room_factory, url, default_size):
    make_messages(room_factory, [DATE] * 105, url == VECTORS)
    assert len(client.get(url).data["results"]) == default_size
    assert len(client.get(url + "?page-size=1000").data["results"]) == 100
    assert len(client.get(url + "?page-size=3").data["results"]) == 3


@pytest.mark.parametrize("url", [MESSAGES, VECTORS])
@pytest.mark.parametrize("query", ["page=1", "page=2", "cursor=invalid", "cursor="])
def test_invalid_pagination_returns_traced_400(client, url, query):
    response = client.get(url + "?" + query)
    assert response.status_code == 400
    assert response.data["token"] is not None
    assert response.data["errors"]


def test_signature_and_context_validation(client, room_factory):
    make_messages(room_factory, [DATE] * 3)
    response = client.get(MESSAGES + "?tag=research&page-size=1")
    link = response.data["next"]
    cursor = parse_qs(urlsplit(link).query)["cursor"][0]
    payload = signing.loads(cursor, salt=CursorPaginator.cursor_salt)
    assert payload["position"] == [str(DATE), str(UUID(int=1))]
    assert "count" not in payload
    assert "offset" not in payload
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    assert client.get(replace_query(link, cursor=tampered)).status_code == 400
    assert client.get(replace_query(link, tag="news")).status_code == 400
    assert client.get(link.replace(MESSAGES, VECTORS)).status_code == 400
    assert client.get(replace_query(link, **{"page-size": 2})).status_code == 200
    for changes in [
        {"version": 99},
        {"version": True},
        {"version": 1.0},
        {"reverse": "false"},
        {"position": [None, None]},
        {"position": ["not-a-date", str(UUID(int=1))]},
        {"position": []},
        {"context": {}},
    ]:
        invalid = signing.dumps({**payload, **changes}, salt=CursorPaginator.cursor_salt)
        assert client.get(replace_query(link, cursor=invalid)).status_code == 400


@pytest.mark.parametrize("url", [MESSAGES, VECTORS, MESSAGES + "/count", VECTORS + "/count"])
def test_read_routes_allow_authenticated_non_staff(api_client, staff_user, url):
    assert api_client.get(url).status_code in {401, 403}
    staff_user.is_staff = False
    staff_user.save(update_fields=["is_staff"])
    api_client.force_authenticate(user=staff_user)
    assert api_client.get(url).status_code == 200


@pytest.mark.parametrize("url", [MESSAGES, VECTORS])
def test_count_reuses_filters_and_executes_only_one_count(client, room_factory, url):
    items = make_messages(room_factory, [DATE] * 4, True)
    MessageItem.objects.filter(pk=items[-1].pk).update(embeddings=None)
    MessageItem.objects.filter(pk=items[1].pk).update(is_reply=True)
    query = "?tag=research&tag=news&tag_match=all&room=test_room&createdat_min=13/10/2025&createdat_max=13/10/2025&is_reply=false"
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url + "/count" + query + "&cursor=ignored&page-size=1")
    assert response.status_code == 200, response.data
    assert set(response.data) == {"count", "token"}
    assert response.data["count"] == (3 if url == MESSAGES else 2)
    assert len([query for query in queries if "COUNT(" in query["sql"].upper()]) == 1
    assert len([query for query in queries if 'INSERT INTO "app_base_query"' in query["sql"]]) == 1
    assert Query.objects.latest("created_at").token == UUID(response.data["token"])
    exported = []
    link = url + query + "&page-size=1"
    while link:
        page = client.get(link)
        assert page.status_code == 200
        exported.extend(page.data["results"])
        link = page.data["next"]
    assert len(exported) == response.data["count"]
    assert client.get(url + "/count?tag=missing").data["count"] == 0
    invalid = client.get(url + "/count?createdat_min=wrong")
    assert invalid.status_code == 400
    assert invalid.data["token"] is not None


def test_count_mixin_is_reusable_without_a_trace_token():
    queryset = Mock()
    filtered = Mock()
    filtered.count.return_value = 7

    class OtherItems(CountMixin, viewsets.GenericViewSet):
        def get_queryset(self):
            return queryset

        def filter_queryset(self, selection):
            assert selection is queryset
            return filtered

    response = OtherItems().count(SimpleNamespace())
    assert response.data == {"count": 7, "token": None}
    filtered.count.assert_called_once_with()


def test_message_filters_are_parsed_and_logged_once(client, room_factory, monkeypatch):
    make_messages(room_factory, [DATE])

    def repeated_selection(self, queryset):
        self.get_queryset()
        return queryset

    monkeypatch.setattr(MessageView, "filter_queryset", repeated_selection)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(MESSAGES + "/count")
    assert response.status_code == 200
    assert len([query for query in queries if 'INSERT INTO "app_base_query"' in query["sql"]]) == 1


def test_helper_defaults_to_existing_page_number_pagination(staff_user):
    class ItemSerializer(serializers.Serializer):
        value = serializers.IntegerField()

    request = Request(APIRequestFactory().get("/other-items?page=2"))
    request.user = staff_user
    response = create_paginate_response(
        token=None, queryset=[{"value": index} for index in range(25)],
        request=request, serializer_class=ItemSerializer,
    )
    assert response.data["count"] == 25
    assert response.data["results"] == [{"value": index} for index in range(10, 20)]
    assert "page=3" in response.data["next"]


def test_openapi_distinguishes_cursors_from_counts():
    router = SimpleRouter(trailing_slash=False)
    router.register("tg/msg", MessageView, basename="messages")
    schema = SchemaGenerator(patterns=[path("api/v1/", include(router.urls))]).get_schema(
        request=None, public=True,
    )
    for url in [MESSAGES, VECTORS]:
        operation = schema["paths"][url]["get"]
        parameters = {parameter["name"] for parameter in operation["parameters"]}
        assert {"cursor", "page-size", "tag"} <= parameters
        assert "page" not in parameters
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        component = schema["components"]["schemas"][response["$ref"].split("/")[-1]]
        assert set(component["properties"]) == {"next", "previous", "results", "token"}
        count = schema["paths"][url + "/count"]["get"]
        parameters = {parameter["name"] for parameter in count["parameters"]}
        assert "tag" in parameters
        assert not {"cursor", "page", "page-size"} & parameters
        response = count["responses"]["200"]["content"]["application/json"]["schema"]
        component = schema["components"]["schemas"][response["$ref"].split("/")[-1]]
        assert set(component["properties"]) == {"count", "token"}


@pytest.mark.parametrize("ordering,expected", [
    (("created_at", "-id"), [5, 3, 6, 2, 4, 1]),
    (("-created_at", "-id"), [2, 6, 5, 3, 4, 1]),
    (("is_reply", "created_at", "id"), [6, 2, 4, 3, 5, 1]),
])
def test_cursor_supports_reusable_composite_ordering(staff_user, room_factory, ordering, expected):
    make_messages(room_factory, [None, DATE + timedelta(days=2), DATE, None, DATE, DATE + timedelta(days=1)])
    MessageItem.objects.filter(id__in=[UUID(int=value) for value in [1, 3, 5]]).update(is_reply=True)

    class ItemSerializer(serializers.Serializer):
        id = serializers.UUIDField()

    def retrieve(link):
        request = Request(APIRequestFactory().get(link))
        request.user = staff_user
        return create_paginate_response(
            token=None, queryset=MessageItem.objects.all(), request=request,
            serializer_class=ItemSerializer, max_items_page=2,
            pagination_class=CursorPaginator, pagination_kwargs={"ordering": ordering},
        )

    pages = []
    link = "/items"
    while link:
        response = retrieve(link)
        pages.append([UUID(item["id"]).int for item in response.data["results"]])
        link = response.data["next"]
    assert [item for page in pages for item in page] == expected
    for page in reversed(pages[:-1]):
        response = retrieve(response.data["previous"])
        assert [UUID(item["id"]).int for item in response.data["results"]] == page
    assert response.data["previous"] is None


def test_cursor_default_unique_ordering_works_for_another_model(staff_user):
    tokens = [UUID(int=value) for value in [3, 1, 2]]
    Query.objects.bulk_create([Query(token=token) for token in tokens])

    class ItemSerializer(serializers.Serializer):
        token = serializers.UUIDField()

    found = []
    link = "/queries"
    while link:
        request = Request(APIRequestFactory().get(link))
        request.user = staff_user
        response = create_paginate_response(
            token=None, queryset=Query.objects.all(), request=request,
            serializer_class=ItemSerializer, max_items_page=1,
            pagination_class=CursorPaginator,
        )
        found.extend(UUID(item["token"]).int for item in response.data["results"])
        link = response.data["next"]
    assert found == [1, 2, 3]
