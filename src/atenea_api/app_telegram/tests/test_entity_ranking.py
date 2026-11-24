"""Integration coverage for ClickHouse-backed entity ranking views."""

import uuid
from unittest.mock import patch

import pytest
from django.urls import reverse

from app_entity.enums import EntityType
from app_entity.models import EntityItem
from app_telegram.serializers import EntityRankingResultSerializer


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("route", "patch_target"),
    [
        ("msg-staff-entities", "app_telegram.services.analytics.entity_ranking"),
        ("app_dashboard:entities", "app_dashboard.views.entity_ranking"),
    ],
)
def test_ranking_views_use_the_clickhouse_fact_service(
    route, patch_target, api_client, staff_user, room_factory,
):
    room = room_factory(
        unique_name="analytics_view_room",
        link="https://t.me/analytics_view_room",
    )
    entity = EntityItem.objects.create(
        unique_ent="analytics_view_alice_per",
        name="Alice Example",
        type=EntityType.PER,
    )
    api_client.force_login(staff_user)
    api_client.force_authenticate(user=staff_user)
    params = {"room": [room.unique_name], "entity_type": ["PER"]}
    rows = [{
        "entity_id": entity.pk,
        "message_count": 1,
        "appearance_count": 3,
    }]

    with patch(patch_target, return_value=rows) as ranking:
        response = api_client.get(reverse(route), params)

    assert response.status_code == 200
    result = response.json()["results"]
    assert len(result) == 1
    assert result[0]["entity_id"] == str(entity.pk)
    assert (result[0]["message_count"], result[0]["appearance_count"]) == (1, 3)
    ranking.assert_called_once()


def test_entity_result_serializer_keeps_the_api_type_textual():
    entity_id = uuid.uuid4()
    result = EntityRankingResultSerializer({
        "entity_id": entity_id,
        "name": "Alice Example",
        "type": EntityType.PER,
        "message_count": 1,
        "appearance_count": 2,
        "messages_url": "/messages",
    })

    assert result.data["type"] == "PER"

    request_data = {
        "entity_id": str(entity_id),
        "name": "Alice Example",
        "type": "PER",
        "message_count": 1,
        "appearance_count": 2,
        "messages_url": "/messages",
    }
    input_serializer = EntityRankingResultSerializer(data=request_data)
    assert input_serializer.is_valid()
    assert input_serializer.validated_data["type"] == "PER"
