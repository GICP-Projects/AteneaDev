from unittest.mock import AsyncMock, patch

import pytest

from app_entity.models import AnnotatedEntity, EntityItem
from app_entity.services.ner import call_ner_service, ner_db_store, ner_extraction


def test_call_ner_service_builds_language_payloads_in_order():
    responses = [
        [{"id": "one", "entities": []}],
        [{"id": "two", "entities": []}],
    ]

    with patch(
        "app_entity.services.ner.call_service",
        new=AsyncMock(return_value=responses),
    ) as call_service:
        result = list(
            __import__("asyncio").run(
                call_ner_service(
                    {
                        "es": [{"pk": "one", "text": "Madrid"}],
                        "en": [{"pk": "two", "text": "London"}],
                    },
                    max_by_request=1,
                    max_parallel_requests=2,
                )
            )
        )

    assert [item["id"] for item in result] == ["one", "two"]
    kwargs = call_service.await_args.kwargs
    es_payload = kwargs["payload_builder_func"](
        [{"pk": "one", "text": "Madrid"}],
        lang="es",
        **kwargs["payload_builder_kwargs"],
    )
    assert es_payload["lang"] == "es"
    assert es_payload["data"] == [{"text": "Madrid", "id": "one"}]
    assert "MISC" in es_payload["types"]


def test_call_ner_service_skips_empty_texts_without_rejecting_the_batch():
    with patch(
        "app_entity.services.ner.call_service",
        new=AsyncMock(return_value=[[{"id": "valid", "entities": []}]]),
    ) as call_service:
        result = list(
            __import__("asyncio").run(
                call_ner_service(
                    {
                        "en": [
                            {"pk": "empty", "text": ""},
                            {"pk": "blank", "text": "   "},
                            {"pk": "null", "text": None},
                            {"pk": "valid", "text": "London"},
                        ]
                    },
                    max_by_request=100,
                    max_parallel_requests=1,
                )
            )
        )

    assert result == [{"id": "valid", "entities": []}]
    kwargs = call_service.await_args.kwargs
    payloads = kwargs["payload_iterate_func"](
        kwargs["data"],
        kwargs["max_by_request"],
        kwargs["payload_builder_func"],
        kwargs["payload_builder_kwargs"],
    )
    assert payloads == [
        {
            "lang": "en",
            "types": [entity_type[0] for entity_type in EntityItem.SPACY_TYPES],
            "data": [{"text": "London", "id": "valid"}],
        }
    ]


@pytest.mark.django_db
def test_ner_extraction_combines_custom_and_service_entities(message_factory):
    message = message_factory(text="Contact @alice in Madrid", lang="en")
    service_result = [
        {
            "id": message.pk.hex,
            "entities": [
                {"name": "Madrid", "type": "GPE", "start_offset": 12, "end_offset": 18}
            ],
        }
    ]

    with patch(
        "app_entity.services.ner.call_ner_service",
        new=AsyncMock(return_value=service_result),
    ):
        entities, annotations, item_pks = ner_extraction(
            {"en": [{"pk": message.pk, "text": message.text}]},
            available_lang=["en"],
        )

    unique_entities = {entity["unique_ent"] for entity in entities}
    assert "@alice_mention" in unique_entities
    assert "madrid_gpe" in unique_entities
    assert annotations["madrid_gpe"][0]["item_pk"] == message.pk.hex
    assert item_pks == [message.pk]


@pytest.mark.django_db(transaction=True)
def test_ner_db_store_persists_misc_entities(message_factory):
    message = message_factory(text="Project Atenea")
    payload = (
        [{"name": "Atenea", "unique_ent": "atenea_misc", "type": "MISC"}],
        {
            "atenea_misc": [
                {"item_pk": message.pk, "start_offset": 8, "end_offset": 14}
            ]
        },
        [message.pk],
    )

    ner_db_store(payload, "MessageItem", "app_telegram")

    entity_item = EntityItem.objects.get(unique_ent="atenea_misc")
    assert entity_item.type == EntityItem.MISC
    assert AnnotatedEntity.objects.filter(
        entity=entity_item,
        object_id=message.pk,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_ner_db_store_is_idempotent_for_same_annotation(message_factory):
    message = message_factory(text="Madrid")
    payload = (
        [{"name": "Madrid", "unique_ent": "madrid_gpe", "type": "GPE"}],
        {
            "madrid_gpe": [
                {"item_pk": message.pk, "start_offset": 0, "end_offset": 6}
            ]
        },
        [message.pk],
    )

    first = ner_db_store(payload, "MessageItem", "app_telegram")
    second = ner_db_store(payload, "MessageItem", "app_telegram")

    assert first == second
    assert EntityItem.objects.filter(unique_ent="madrid_gpe").count() == 1
    assert AnnotatedEntity.objects.filter(object_id=message.pk).count() == 1
