"""PostgreSQL integration coverage for durable search and analytics projection work.

The database is real in these tests; Celery dispatch and Elasticsearch HTTP
calls are replaced so a test can exercise the signal, job, and retry protocol
without reaching a broker or cluster.
"""

from unittest.mock import MagicMock, patch
import uuid

import pytest
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.utils import timezone
from django.test import override_settings

from app_entity.enums import EntityType
from app_entity.models import AnnotatedEntity, EntityItem
from app_telegram.services import analytics
from app_telegram.services.jobs import queue_index_sync
from app_entity.services.ner import store_ner_results
from app_telegram.tasks import sync_message_entity_index_batch
from app_scheduler.models import ProcessingBatch, ProcessingJob
from app_scheduler.services.jobs import create_batch_locked, create_processing_job
from app_telegram.models import MessageItem
from app_telegram.job_keys import (
    TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR,
    TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE,
)
from app_telegram.services import index as telegram_index


pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _entity(name, entity_type=EntityType.GPE):
    normalized_name = name.lower().replace(" ", "_")
    entity_kind = EntityType.to_name(entity_type).lower()
    unique_ent = f"{normalized_name}_{entity_kind}"
    return EntityItem.objects.create(
        unique_ent=unique_ent,
        name=name,
        type=entity_type,
    )


def _annotation(message, entity, start_offset, end_offset):
    return AnnotatedEntity.objects.create(
        entity=entity,
        content_type=ContentType.objects.get_for_model(MessageItem),
        object_id=message.pk,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def _index_job(message, *, operation="messages"):
    job, created = create_processing_job(
        processor=TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR,
        mode=ProcessingJob.BACKFILL,
        workflow=[TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE],
        filters={"operation": operation},
        batch_size=10,
        max_inflight_batches=1,
        deduplicate=False,
    )
    assert created
    batch = create_batch_locked(
        job,
        item_ids=[str(message.pk)],
        item_content_type=ContentType.objects.get_for_model(MessageItem),
        schedule_dispatch=False,
    )
    return job, batch


def test_message_delete_job_and_batch_roll_back_with_source_delete(message_factory):
    message = message_factory()
    message_id = message.pk

    class RollBackDelete(Exception):
        pass

    with override_settings(
        INDEX_MANUAL_BATCH_SIZE=10,
    ), patch("app_scheduler.services.jobs.dispatch_ready_batches") as dispatch:
        with pytest.raises(RollBackDelete):
            with transaction.atomic():
                message.delete()

                job = ProcessingJob.objects.get(
                    processor=TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR,
                    filters__operation="delete_messages",
                )
                batch = ProcessingBatch.objects.get(job=job)
                assert batch.item_ids == [str(message_id)]
                assert job.source_exhausted is True
                assert job.input_closed_at is not None

                raise RollBackDelete

        assert MessageItem.objects.filter(pk=message_id).exists()
        assert not ProcessingJob.objects.filter(processor=TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR).exists()
        dispatch.assert_not_called()


def test_entity_cascade_queues_message_chunks_and_entity_cleanup_without_annotation_jobs(
    room_factory, message_factory,
):
    room = room_factory(tg_id=71001, unique_name="cascade_room")
    first = message_factory(
        room=room, msg_id=101, link="https://t.me/cascade_room/101",
    )
    second = message_factory(
        room=room, msg_id=102, link="https://t.me/cascade_room/102",
    )
    entity = _entity("Cascade Entity")
    entity_id = entity.pk
    _annotation(first, entity, 0, 4)
    _annotation(second, entity, 5, 9)

    with override_settings(
        INDEX_MANUAL_BATCH_SIZE=1,
    ), patch("app_scheduler.services.jobs.dispatch_ready_batches") as dispatch:
        entity.delete()

        jobs = list(ProcessingJob.objects.filter(processor=TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR))
        assert len(jobs) == 3
        operations = [job.filters["operation"] for job in jobs]
        assert operations.count("messages") == 2
        assert operations.count("delete_entities") == 1

        message_batches = list(
            ProcessingBatch.objects.filter(job__filters__operation="messages")
            .values_list("item_ids", flat=True)
        )
        assert {tuple(item_ids) for item_ids in message_batches} == {
            (str(first.pk),),
            (str(second.pk),),
        }
        entity_batch = ProcessingBatch.objects.get(job__filters__operation="delete_entities")
        assert entity_batch.item_ids == [str(entity_id)]
        assert dispatch.call_count == 3


def test_repeated_identical_sync_events_are_persisted_as_separate_jobs(message_factory):
    message = message_factory()

    with override_settings(
        INDEX_MANUAL_BATCH_SIZE=10,
    ), patch("app_scheduler.services.jobs.dispatch_ready_batches") as dispatch:
        first_job = queue_index_sync([message.pk], operation="messages")
        second_job = queue_index_sync([message.pk], operation="messages")

    assert first_job.pk != second_job.pk
    jobs = list(
        ProcessingJob.objects.filter(
            processor=TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR,
            filters__operation="messages",
        )
    )
    assert {job.pk for job in jobs} == {first_job.pk, second_job.pk}
    assert list(
        ProcessingBatch.objects.filter(job_id__in=[first_job.pk, second_job.pk])
        .values_list("item_ids", flat=True)
    ) == [[str(message.pk)], [str(message.pk)]]
    assert dispatch.call_count == 2


def test_ner_annotation_replacement_suppresses_delete_projection_jobs(message_factory):
    message = message_factory(text="Madrid and London")
    old_entity = _entity("Madrid")
    _annotation(message, old_entity, 0, 6)

    replacement = (
        [{"name": "London", "unique_ent": "london_gpe", "type": "GPE"}],
        {"london_gpe": [{"item_pk": message.pk, "start_offset": 11, "end_offset": 17}]},
        [message.pk],
    )

    with override_settings(
        INDEX_MANUAL_BATCH_SIZE=10,
    ):
        store_ner_results(
            replacement,
            "MessageItem",
            "app_telegram",
            replace_message_ids=[message.pk],
        )

    assert list(
        AnnotatedEntity.objects.filter(object_id=message.pk)
        .select_related("entity")
        .values_list("entity__unique_ent", flat=True)
    ) == ["london_gpe"]
    assert telegram_index.build_message_entity_facts([message.pk])[0]["count"] == 1
    assert not ProcessingJob.objects.filter(processor=TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR).exists()


def test_direct_annotation_delete_queues_projection_of_remaining_occurrences(message_factory):
    message = message_factory()
    entity = _entity("Repeated Name")
    first = _annotation(message, entity, 0, 6)
    _annotation(message, entity, 12, 18)

    with override_settings(INDEX_MANUAL_BATCH_SIZE=10), patch(
        "app_scheduler.services.jobs.dispatch_ready_batches",
    ) as dispatch:
        first.delete()

        [fact] = telegram_index.build_message_entity_facts([message.pk])
        assert fact["count"] == 1
        job = ProcessingJob.objects.get(
            processor=TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR,
            filters__operation="messages",
        )
        assert ProcessingBatch.objects.get(job=job).item_ids == [str(message.pk)]
        dispatch.assert_called_once_with(job.pk)




def test_builder_groups_occurrences_and_uses_uuid_and_enum_type(message_factory):
    message = message_factory(msg_id=7821, is_reply=True, created_at=timezone.now())
    entity = _entity("Example Org", EntityType.ORG)
    _annotation(message, entity, 0, 4)
    _annotation(message, entity, 8, 12)

    [fact] = telegram_index.build_message_entity_facts([message.pk, message.pk])

    assert fact == {
        "msg_id": str(message.pk),
        "entity_id": str(entity.pk),
        "room_id": str(message.room_id),
        "type": EntityType.ORG.value,
        "count": 2,
        "created_at": message.created_at,
        "stored_date": message.stored_date,
        "is_reply": True,
    }
    assert fact["msg_id"] != str(message.msg_id)


def test_reconcile_deletes_facts_for_empty_or_missing_messages(message_factory):
    empty_message = message_factory(msg_id=911)
    orphan_id = uuid.uuid4()
    orphan_entity = _entity("Orphan Source")
    AnnotatedEntity.objects.create(
        entity=orphan_entity,
        content_type=ContentType.objects.get_for_model(MessageItem),
        object_id=orphan_id,
        start_offset=0,
        end_offset=5,
    )
    with patch.object(telegram_index, "_reconcile_message_entities", return_value=0) as reconcile:
        written = telegram_index.reconcile_message_entities([empty_message.pk, orphan_id])

    assert written == 0
    reconcile.assert_called_once_with([str(empty_message.pk), str(orphan_id)], using="default", client=None)


def test_index_job_retries_bulk_errors_and_keeps_the_stage_incomplete(message_factory):
    message = message_factory(msg_id=1001)
    job, batch = _index_job(message)
    client = MagicMock()

    class RetryRequested(Exception):
        pass

    def fail_bulk(_client, actions, **kwargs):
        assert _client is client
        assert not connection.in_atomic_block
        assert len(list(actions)) == 1
        return 0, [{"index": {"status": 503, "error": {"type": "unavailable"}}}]

    with override_settings(
        BULK_BATCH_SIZE=10,
    ), patch(
        "elasticsearch.dsl.connections.get_connection", return_value=client,
    ), patch.object(
        telegram_index.MessageDocument,
        "get_queryset",
        return_value=[message],
    ), patch.object(
        telegram_index.MessageDocument,
        "_prepare_action",
        return_value={"_op_type": "index", "_id": str(message.pk)},
    ), patch(
        "elasticsearch.helpers.bulk", side_effect=fail_bulk,
    ) as bulk_write, patch.object(
        sync_message_entity_index_batch,
        "retry",
        side_effect=RetryRequested("retry requested"),
    ):
        with pytest.raises(RetryRequested):
            sync_message_entity_index_batch.run(batch.pk)

    batch.refresh_from_db()
    job.refresh_from_db()
    assert bulk_write.called
    assert batch.status == ProcessingBatch.RETRY_WAIT
    assert batch.current_stage == TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE
    assert batch.completed_stages == []
    assert batch.processed_count == 0
    assert batch.failed_count == 0
    assert batch.stage_attempts[TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE] == 1
    assert job.status != ProcessingJob.COMPLETED






def test_disabled_message_rewrite_still_reconciles_entity_facts(message_factory):
    message = message_factory(msg_id=1201)
    client = MagicMock()

    with override_settings(
        BULK_BATCH_SIZE=10,
    ), patch(
        "elasticsearch.dsl.connections.get_connection", return_value=client,
    ), patch.object(
        telegram_index, "reconcile_message_entities", return_value=3,
    ) as reconcile, patch.object(telegram_index.MessageDocument, "get_queryset") as get_queryset:
        counts = telegram_index.index_message_ids([message.pk], rewrite_messages=False)

    get_queryset.assert_not_called()
    reconcile.assert_called_once_with(
        [str(message.pk)],
        block_size=settings.ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE,
    )
    assert counts == {"search_messages_indexed": 0, "analytics_entity_facts_synchronized": 3}


def test_index_counts_acknowledged_search_documents_and_current_facts():
    first, second, missing = (str(uuid.uuid4()) for _ in range(3))
    actions = [
        [{"_op_type": "index", "_id": first}, {"_op_type": "index", "_id": second}],
        [{"_op_type": "delete", "_id": missing}],
    ]
    with override_settings(ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE=2), patch.object(
        telegram_index, "_client",
    ), patch.object(
        telegram_index, "_build_model_actions", side_effect=actions,
    ), patch.object(
        telegram_index, "_write_actions",
    ) as write, patch.object(
        telegram_index, "_refresh_index",
    ) as refresh, patch.object(
        telegram_index, "reconcile_message_entities", side_effect=[4, 0],
    ) as reconcile:
        counts = telegram_index.index_message_ids([first, first, second, missing])

    assert counts == {"search_messages_indexed": 2, "analytics_entity_facts_synchronized": 4}
    assert write.call_count == reconcile.call_count == 2
    assert reconcile.call_args_list[0].args[0] == [first, second]
    assert reconcile.call_args_list[1].args[0] == [missing]
    refresh.assert_called_once()


def test_analytics_failure_does_not_return_a_successful_index_summary():
    with patch.object(telegram_index, "_client"), patch.object(
        telegram_index, "_build_model_actions", return_value=[{"_op_type": "index"}],
    ), patch.object(telegram_index, "_write_actions"), patch.object(
        telegram_index, "_refresh_index",
    ) as refresh, patch.object(
        telegram_index, "reconcile_message_entities", side_effect=RuntimeError("ClickHouse unavailable"),
    ), pytest.raises(RuntimeError, match="ClickHouse unavailable"):
        telegram_index.index_message_ids([uuid.uuid4()])
    refresh.assert_not_called()


def test_shared_message_filter_uses_current_room_tags_and_case_insensitive_regex(
    room_factory,
):
    matching = room_factory(
        tg_id=72001,
        unique_name="CryptoAlerts",
        link="https://t.me/CryptoAlerts",
        tags=["news", "tokens"],
    )
    newly_tagged = room_factory(
        tg_id=72002,
        unique_name="cryptodesk",
        link="https://t.me/cryptodesk",
        tags=["research"],
    )
    room_factory(
        tg_id=72003,
        unique_name="OtherAlerts",
        link="https://t.me/OtherAlerts",
        tags=["news"],
    )

    # The filter resolves against current PostgreSQL room metadata, so a tag
    # update takes effect without rebuilding the analytical facts.
    first_query = analytics.build_message_filter(
        room=["crypto(alerts|desk)"], tags=["news"],
    ).to_dict()
    first_sql, first_params = analytics.build_entity_ranking_query(
        {"room": ["crypto(alerts|desk)"], "tags": ["news"]},
    )
    newly_tagged.tags = ["news"]
    newly_tagged.save(update_fields=["tags"])
    updated_query = analytics.build_message_filter(
        room=["crypto(alerts|desk)"], tags=["news"],
    ).to_dict()
    updated_sql, updated_params = analytics.build_entity_ranking_query(
        {"room": ["crypto(alerts|desk)"], "tags": ["news"]},
    )

    def room_ids(query):
        return query["bool"]["filter"][0]["terms"]["room_id"]

    assert room_ids(first_query) == [str(matching.pk)]
    assert set(room_ids(updated_query)) == {str(matching.pk), str(newly_tagged.pk)}
    assert "room_id IN {rooms:Array(UUID)}" in first_sql
    assert "room_id IN {rooms:Array(UUID)}" in updated_sql
    assert first_params["rooms"] == [str(matching.pk)]
    assert set(updated_params["rooms"]) == {str(matching.pk), str(newly_tagged.pk)}
