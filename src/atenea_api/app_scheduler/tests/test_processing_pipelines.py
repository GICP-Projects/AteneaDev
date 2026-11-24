from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from celery.exceptions import Retry
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError, connections, transaction
from django.test import override_settings
from django.utils import timezone

from app_base.models import Query
from app_base.signals import OnlyDeleteSignalES
from app_entity.ner_state import NER_INPUT_NORMALIZER_VERSION, build_ner_input_hash
from app_entity.models import NERBatchResult, NERState
from app_entity.services.ner import NER_CUSTOM_ENTITIES_VERSION
from app_entity.services.jobs import (
    NER_EXTRACT_STAGE,
    NER_STORE_STAGE,
    build_ner_batch_metadata,
    claim_batch_messages_locked,
    create_ner_backfill_job,
    dispatch_ner_backfill,
    state_is_claimable,
    validate_ner_batch_size,
)
from app_entity.tasks import extract_ner_batch, store_ner_batch
from app_metadata.job_keys import (
    EMBEDDINGS_PROCESSOR,
    EMBEDDINGS_STAGE,
    SENTIMENT_PROCESSOR,
    SENTIMENT_STAGE,
)
from app_metadata.tasks import embed_processing_batch, sentiment_processing_batch
from app_scheduler.models import ProcessingBatch, ProcessingHandoff, ProcessingJob
from app_scheduler.tests.test_database_concurrency import deadlock
from app_scheduler.services.jobs import (
    begin_batch_stage_locked,
    cancel_job,
    complete_batch_stage_locked,
    create_batch_locked,
    create_handoffs_locked,
    create_processing_job,
    discard_failed_job,
    dispatch_handoffs,
    fail_batch_stage_locked,
    InvalidJobAction,
    pause_job,
    refresh_job_locked,
    retry_failed_job,
    resume_job,
    seal_job,
)
from app_telegram.models import MessageItem, RoomItem, SeedItem
from app_telegram.job_keys import (
    TELEGRAM_INDEX_MESSAGES_PROCESSOR,
    TELEGRAM_INDEX_ROOMS_STAGE,
    TELEGRAM_POPULATE_PROCESSOR,
)
from app_telegram.services.jobs import (
    TELEGRAM_INDEX_STAGE,
    TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
    TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
    TELEGRAM_SCAN_PROCESSOR,
    TELEGRAM_SCAN_STAGE,
)
from app_telegram.tasks import (
    index_messages_processing_batch,
    index_rooms_from_seeds_processing_batch,
)


METADATA = {
    "service_version": "test-service",
    "spacy_version": "3.test",
    "models": {"en": {"name": "en_test", "version": "1"}},
    "schema_version": "ner-output-v1",
}


def _metadata(version="test-version"):
    return version, {"api_ner": METADATA, "marker": version}


def test_ner_batch_size_has_no_second_environment_cap():
    assert validate_ner_batch_size(10_001) == 10_001
    with pytest.raises(ValueError, match="positive"):
        validate_ner_batch_size(0)


def test_ner_batch_version_uses_code_versions_and_api_schema_metadata():
    version, components = build_ner_batch_metadata(METADATA)

    assert version
    assert components["api_ner"]["schema_version"] == "ner-output-v1"
    assert components["custom_entities_version"] == NER_CUSTOM_ENTITIES_VERSION
    assert components["input_normalizer_version"] == NER_INPUT_NORMALIZER_VERSION
    assert "output_schema_version" not in components


def _create_live_message_job():
    job, _ = create_processing_job(
        processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
        mode=ProcessingJob.LIVE,
        workflow=TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
        filters={},
        batch_size=10_000,
        max_inflight_batches=1,
        fingerprint=uuid4().hex,
        deduplicate=False,
    )
    return job


def _create_message_batch(job, message_ids):
    with transaction.atomic():
        locked = ProcessingJob.objects.select_for_update().get(pk=job.pk)
        batch = create_batch_locked(
            locked,
            item_ids=[str(item_id) for item_id in message_ids],
            item_content_type=ContentType.objects.get_for_model(MessageItem, for_concrete_model=False),
        )
        refresh_job_locked(locked)
    return batch


@pytest.mark.django_db
def test_equivalent_manual_ner_backfill_is_deduplicated(message_factory):
    message_factory(annotated_text="London", stored_date=timezone.now())

    with patch("app_entity.services.jobs.enqueue_source_dispatch"):
        first, first_created = create_ner_backfill_job(filters={"stored_since": "2026-07-17"})
        second, second_created = create_ner_backfill_job(filters={"stored_since": "2026-07-17"})

    assert first_created is True
    assert second_created is False
    assert second.pk == first.pk
    assert first.workflow == [NER_EXTRACT_STAGE, NER_STORE_STAGE, TELEGRAM_INDEX_STAGE]
    assert first.filters["reindex_messages"] is False
    assert first.filters["stored_since"].startswith("2026-07-17")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("stage", [NER_EXTRACT_STAGE, NER_STORE_STAGE])
def test_ner_commit_deadlock_preserves_payload_without_repeating_http(message_factory, monkeypatch, stage):
    message = message_factory(annotated_text="London", stored_date=timezone.now())
    connection = connections["default"]
    commit = connection.commit
    failed = False

    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_entity.tasks.extract_ner_data", return_value=([], {}, [message.pk]),
    ) as extract, patch("app_entity.tasks.build_ner_batch_metadata", return_value=_metadata()), patch(
        "app_entity.tasks.store_ner_results",
        return_value={"MessageItem": {"and_filter_fields": {"pk__in": [message.pk]}}},
    ) as store:
        owner = _create_live_message_job()
        source = _create_message_batch(owner, [message.pk])
        if stage == NER_STORE_STAGE:
            assert extract_ner_batch.run(source.pk)["status"] == "extracted"

        def commit_results():
            nonlocal failed
            progress = ProcessingBatch.objects.values_list("completed_stages", flat=True).get(pk=source.pk)
            if not failed and stage in progress:
                failed = True
                raise deadlock()
            return commit()

        monkeypatch.setattr(connection, "commit", commit_results)
        result = extract_ner_batch.run(source.pk) if stage == NER_EXTRACT_STAGE else store_ner_batch.run(source.pk)
        assert result["status"] == ("extracted" if stage == NER_EXTRACT_STAGE else "stored")
        assert failed
        extract.assert_called_once()
        assert store.call_count == (0 if stage == NER_EXTRACT_STAGE else 2)
        source.refresh_from_db()
        assert source.stage_attempts[stage] == 1
        assert source.current_stage == (NER_STORE_STAGE if stage == NER_EXTRACT_STAGE else TELEGRAM_INDEX_STAGE)
        assert NERBatchResult.objects.filter(batch=source).exists() is (stage == NER_EXTRACT_STAGE)


@pytest.mark.django_db(transaction=True)
def test_legacy_ner_job_gains_projection_stage_after_store(message_factory):
    message = message_factory(annotated_text="London", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        job.workflow = [NER_EXTRACT_STAGE, NER_STORE_STAGE]
        job.save(update_fields=["workflow"])
        batch = _create_message_batch(job, [message.pk])
        with patch("app_entity.tasks.extract_ner_data", return_value=([], {}, [message.pk])), patch(
            "app_entity.tasks.build_ner_batch_metadata", return_value=_metadata(),
        ):
            assert extract_ner_batch.run(batch.pk)["status"] == "extracted"
        with patch(
            "app_entity.tasks.store_ner_results",
            return_value={"MessageItem": {"and_filter_fields": {"pk__in": [message.pk]}}},
        ):
            assert store_ner_batch.run(batch.pk)["status"] == "stored"
    job.refresh_from_db()
    batch.refresh_from_db()
    assert job.workflow == [NER_EXTRACT_STAGE, NER_STORE_STAGE, TELEGRAM_INDEX_STAGE]
    assert job.filters["reindex_messages"] is False
    assert batch.current_stage == TELEGRAM_INDEX_STAGE


@pytest.mark.django_db
def test_room_delete_signal_keeps_generic_es_deletes_enabled(room_factory):
    room = room_factory()
    processor = object.__new__(OnlyDeleteSignalES)
    processor._logged_signal_activations = set()

    with patch(
        "django_elasticsearch_dsl.signals.BaseSignalProcessor.handle_delete",
    ) as generic_delete:
        processor.handle_delete(RoomItem, room)

    generic_delete.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_message_and_room_index_batches_always_run(message_factory, seed_factory):
    message = message_factory(msg_id=6101)
    seed = seed_factory(link="https://t.me/index-seed")
    message_job, _ = create_processing_job(
        processor=TELEGRAM_INDEX_MESSAGES_PROCESSOR,
        mode=ProcessingJob.BACKFILL,
        workflow=[TELEGRAM_INDEX_STAGE],
        filters={},
        batch_size=1,
        max_inflight_batches=1,
        deduplicate=False,
        fingerprint=uuid4().hex,
    )
    room_job, _ = create_processing_job(
        processor=TELEGRAM_POPULATE_PROCESSOR,
        mode=ProcessingJob.BACKFILL,
        workflow=[TELEGRAM_INDEX_ROOMS_STAGE],
        filters={},
        batch_size=1,
        max_inflight_batches=1,
        deduplicate=False,
        fingerprint=uuid4().hex,
    )
    with transaction.atomic():
        message_batch = create_batch_locked(
            ProcessingJob.objects.select_for_update().get(pk=message_job.pk),
            item_ids=[str(message.pk)],
            item_content_type=ContentType.objects.get_for_model(MessageItem),
            schedule_dispatch=False,
        )
        room_batch = create_batch_locked(
            ProcessingJob.objects.select_for_update().get(pk=room_job.pk),
            item_ids=[str(seed.pk)],
            item_content_type=ContentType.objects.get_for_model(SeedItem),
            schedule_dispatch=False,
        )

    with patch("app_telegram.tasks.index_message_ids", return_value={
        "search_messages_indexed": 1, "analytics_entity_facts_synchronized": 0,
    }) as index_messages, \
            patch("app_telegram.tasks.index_room_ids") as index_rooms, \
            patch("app_scheduler.services.jobs.dispatch_ready_batches"), \
            patch("app_scheduler.services.jobs.dispatch_handoffs"), \
            patch("app_scheduler.services.jobs.enqueue_source_dispatch"):
        message_result = index_messages_processing_batch.run(message_batch.pk)
        room_result = index_rooms_from_seeds_processing_batch.run(room_batch.pk)

    assert message_result["status"] == "indexed"
    assert room_result["status"] == "rooms_indexed"
    index_messages.assert_called_once_with([str(message.pk)])
    index_rooms.assert_called_once_with([])


@pytest.mark.django_db(transaction=True)
def test_index_summary_marks_search_as_disabled_while_counting_analytical_facts(message_factory):
    message = message_factory()
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        job.workflow = [TELEGRAM_INDEX_STAGE]
        job.filters = {"reindex_messages": False}
        job.save(update_fields=["workflow", "filters"])
        batch = _create_message_batch(job, [message.pk])
    with patch("app_telegram.tasks.index_message_ids", return_value={
        "search_messages_indexed": 0, "analytics_entity_facts_synchronized": 2,
    }) as index_messages, patch("app_scheduler.services.jobs.dispatch_ready_batches"), \
            patch("app_scheduler.services.jobs.dispatch_handoffs"), \
            patch("app_scheduler.services.jobs.enqueue_source_dispatch"):
        result = index_messages_processing_batch.run(batch.pk)
    assert result["status"] == "indexed"
    index_messages.assert_called_once_with([str(message.pk)], rewrite_messages=False)
    batch.refresh_from_db()
    summary = batch.result_summary["stages"][TELEGRAM_INDEX_STAGE]
    assert summary["metadata"]["search_indexing_enabled"] is False
    assert {metric["key"]: metric["count"] for metric in summary["outputs"]} == {
        "search_messages_indexed": 0, "analytics_entity_facts_synchronized": 2,
    }


@pytest.mark.django_db(transaction=True)
def test_backfill_claims_bounded_immutable_items(message_factory):
    first = message_factory(
        link="https://t.me/test_room/101",
        msg_id=101,
        annotated_text="London",
        stored_date=timezone.now(),
    )
    second = message_factory(
        link="https://t.me/test_room/102",
        msg_id=102,
        room=first.room,
        annotated_text="Madrid",
        stored_date=timezone.now(),
    )

    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_entity.services.jobs.enqueue_source_dispatch"
    ):
        job, _ = create_ner_backfill_job(filters={}, batch_size=2)
        dispatch_ner_backfill(job.id)

    batch = ProcessingBatch.objects.get(job=job)
    content_type = ContentType.objects.get_for_model(first, for_concrete_model=False)
    assert set(batch.item_ids) == {str(first.pk), str(second.pk)}
    assert batch.current_stage == NER_EXTRACT_STAGE
    assert batch.processor_version == ""
    assert NERState.objects.filter(content_type=content_type, claimed_batch=batch).count() == 2


@pytest.mark.django_db(transaction=True)
def test_backfill_snapshot_excludes_messages_stored_after_job_creation(message_factory):
    first = message_factory(
        link="https://t.me/test_room/301",
        msg_id=301,
        annotated_text="Before snapshot",
        stored_date=timezone.now(),
    )
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_entity.services.jobs.enqueue_source_dispatch"
    ):
        job, _ = create_ner_backfill_job(filters={}, batch_size=2)
        later = message_factory(
            link="https://t.me/test_room/302",
            msg_id=302,
            room=first.room,
            annotated_text="After snapshot",
            stored_date=job.source_snapshot_at + timedelta(seconds=1),
        )
        dispatch_ner_backfill(job.id)

    batch = ProcessingBatch.objects.get(job=job)
    assert batch.item_ids == [str(first.id)]
    assert str(later.id) not in batch.item_ids


@pytest.mark.django_db(transaction=True)
def test_backfill_continues_after_a_page_of_already_completed_states(message_factory):
    # Keep all source rows strictly before the Job snapshot.  The production
    # dispatcher must exclude messages that arrive after a backfill starts.
    stored_at = timezone.now() - timedelta(minutes=1)
    first = message_factory(annotated_text="First", stored_date=stored_at)
    messages = [first]
    for index in range(2, 6):
        messages.append(
            message_factory(
                link=f"https://t.me/test_room/{500 + index}",
                msg_id=500 + index,
                room=first.room,
                annotated_text=f"Message {index}",
                stored_date=stored_at + timedelta(seconds=index),
            )
        )
    content_type = ContentType.objects.get_for_model(MessageItem, for_concrete_model=False)
    NERState.objects.bulk_create(
        [
            NERState(
                content_type=content_type,
                object_id=message.id,
                status=NERState.COMPLETED,
            )
            for message in messages
        ]
    )

    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_entity.services.jobs.enqueue_source_dispatch"
    ) as enqueue_source:
        job, _ = create_ner_backfill_job(filters={}, batch_size=1)
        enqueue_source.reset_mock()
        assert dispatch_ner_backfill(job.id) == []

    job.refresh_from_db()
    assert job.source_exhausted is False
    assert job.source_cursor["id"] == str(messages[3].id)
    enqueue_source.assert_called_once_with(job.id)


@pytest.mark.django_db(transaction=True)
@override_settings(NER_BACKFILL_MAX_INFLIGHT=2)
def test_metadata_is_refreshed_per_extracted_batch_not_on_job(message_factory):
    first = message_factory(annotated_text="First", stored_date=timezone.now())
    second = message_factory(
        link="https://t.me/test_room/402",
        msg_id=402,
        room=first.room,
        annotated_text="Second",
        stored_date=timezone.now(),
    )
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_entity.services.jobs.enqueue_source_dispatch"
    ):
        job, _ = create_ner_backfill_job(filters={}, batch_size=1)
        dispatch_ner_backfill(job.id)

    batches = list(ProcessingBatch.objects.filter(job=job).order_by("ordinal"))
    with patch(
        "app_entity.tasks.build_ner_batch_metadata",
        side_effect=[_metadata("metadata-v1"), _metadata("metadata-v2")],
    ), patch(
        "app_entity.tasks.extract_ner_data",
        side_effect=[([], {}, [first.id]), ([], {}, [second.id])],
    ), patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        extract_ner_batch.run(batches[0].id)
        extract_ner_batch.run(batches[1].id)

    for batch in batches:
        batch.refresh_from_db()
    assert [batch.processor_version for batch in batches] == ["metadata-v1", "metadata-v2"]
    assert not hasattr(job, "target_version")


@pytest.mark.django_db(transaction=True)
def test_scan_child_workflow_adds_index_without_making_manual_ner_index(message_factory):
    message = message_factory(annotated_text="Berlin", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [message.id])

    assert job.workflow == TELEGRAM_PROCESS_MESSAGES_WORKFLOW
    assert batch.current_stage == NER_EXTRACT_STAGE
    assert TELEGRAM_INDEX_STAGE in job.workflow


@pytest.mark.django_db(transaction=True)
def test_scan_child_completes_only_after_telegram_index_stage(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [message.id])
        seal_job(job.id)

        for stage in [NER_EXTRACT_STAGE, NER_STORE_STAGE]:
            with transaction.atomic():
                batch = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.id)
                token = begin_batch_stage_locked(batch, stage)
                assert token
                complete_batch_stage_locked(batch, stage=stage, token=token)

        job.refresh_from_db()
        assert job.status == ProcessingJob.RUNNING
        batch.refresh_from_db()
        assert batch.current_stage == TELEGRAM_INDEX_STAGE

        with transaction.atomic():
            batch = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.id)
            token = begin_batch_stage_locked(batch, TELEGRAM_INDEX_STAGE)
            assert token
            complete_batch_stage_locked(batch, stage=TELEGRAM_INDEX_STAGE, token=token)

    job.refresh_from_db()
    assert job.status == ProcessingJob.COMPLETED
    assert job.processed_items == 1


@pytest.mark.django_db(transaction=True)
def test_scan_parent_finishes_independently_and_handoff_starts_early(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        root, _ = create_processing_job(
            processor=TELEGRAM_SCAN_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=[TELEGRAM_SCAN_STAGE],
            filters={},
            batch_size=1,
            max_inflight_batches=1,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        child, _ = create_processing_job(
            processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
            filters={},
            batch_size=1,
            max_inflight_batches=1,
            parent_job=root,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        with transaction.atomic():
            locked_root = ProcessingJob.objects.select_for_update().get(pk=root.id)
            source = create_batch_locked(
                locked_root,
                item_ids=[str(message.room_id)],
                item_content_type=ContentType.objects.get_for_model(RoomItem, for_concrete_model=False),
            )
            create_handoffs_locked(
                source,
                target_job=child,
                item_content_type=ContentType.objects.get_for_model(message, for_concrete_model=False),
                item_ids=[str(message.id)],
            )
            refresh_job_locked(locked_root)
        dispatch_handoffs(child.id)
        child_batch = ProcessingBatch.objects.get(job=child)
        assert child_batch.source_handoff_id
        handoff = ProcessingHandoff.objects.get(target_batch=child_batch)
        assert handoff.status == ProcessingHandoff.MATERIALIZED
        assert handoff.item_count == 1
        assert handoff.item_ids == []
        assert not NERState.objects.filter(object_id=message.id).exists()

        seal_job(root.id)
        with transaction.atomic():
            source = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=source.id)
            token = begin_batch_stage_locked(source, TELEGRAM_SCAN_STAGE)
            assert token
            complete_batch_stage_locked(source, stage=TELEGRAM_SCAN_STAGE, token=token)

    root.refresh_from_db()
    child.refresh_from_db()
    assert root.status == ProcessingJob.COMPLETED
    assert child.status == ProcessingJob.RUNNING


@pytest.mark.django_db(transaction=True)
def test_failed_parent_waits_for_retry_without_sealing_child(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_scheduler.services.jobs.enqueue_handoff_dispatch"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"), patch(
        "app_scheduler.services.jobs.dispatch_ready_batches"
    ), patch("app_scheduler.services.jobs.dispatch_handoffs"):
        root, _ = create_processing_job(
            processor=TELEGRAM_SCAN_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=[TELEGRAM_SCAN_STAGE],
            filters={},
            batch_size=1,
            max_inflight_batches=1,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        child, _ = create_processing_job(
            processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
            filters={},
            batch_size=10_000,
            max_inflight_batches=1,
            parent_job=root,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        with transaction.atomic():
            locked_root = ProcessingJob.objects.select_for_update().get(pk=root.id)
            source = create_batch_locked(
                locked_root,
                item_ids=[str(message.room_id)],
                item_content_type=ContentType.objects.get_for_model(RoomItem, for_concrete_model=False),
            )
            refresh_job_locked(locked_root)

        seal_job(root.id)
        with transaction.atomic():
            source = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=source.id)
            token = begin_batch_stage_locked(source, TELEGRAM_SCAN_STAGE)
            assert token
            fail_batch_stage_locked(
                source,
                stage=TELEGRAM_SCAN_STAGE,
                token=token,
                error="temporary scan failure",
            )

    root.refresh_from_db()
    child.refresh_from_db()
    assert root.status == ProcessingJob.WAITING_RETRY
    assert child.status == ProcessingJob.COLLECTING
    assert child.input_closed_at is None

    retry_failed_job(root.id)

    root.refresh_from_db()
    child.refresh_from_db()
    assert root.status == ProcessingJob.RUNNING
    assert child.status == ProcessingJob.COLLECTING
    assert child.input_closed_at is None
    assert child.source_finished_at is None
    assert child.completed_at is None

    with transaction.atomic():
        source = ProcessingBatch.objects.select_for_update().get(pk=source.id)
        child = ProcessingJob.objects.get(pk=child.id)
        handoffs = create_handoffs_locked(
            source,
            target_job=child,
            item_content_type=ContentType.objects.get_for_model(message, for_concrete_model=False),
            item_ids=[str(message.id)],
        )

    assert len(handoffs) == 1
    assert ProcessingHandoff.objects.filter(target_job=child, status=ProcessingHandoff.PENDING).exists()


@pytest.mark.django_db(transaction=True)
def test_discard_failed_parent_marks_partial_and_completes_child_with_partial_input(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_scheduler.services.jobs.enqueue_handoff_dispatch"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"), patch(
        "app_scheduler.services.jobs.dispatch_ready_batches"
    ), patch("app_scheduler.services.jobs.dispatch_handoffs"):
        root, _ = create_processing_job(
            processor=TELEGRAM_SCAN_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=[TELEGRAM_SCAN_STAGE],
            filters={},
            batch_size=1,
            max_inflight_batches=1,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        child, _ = create_processing_job(
            processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
            filters={},
            batch_size=10_000,
            max_inflight_batches=1,
            parent_job=root,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        with transaction.atomic():
            locked_root = ProcessingJob.objects.select_for_update().get(pk=root.id)
            source = create_batch_locked(
                locked_root,
                item_ids=[str(message.room_id)],
                item_content_type=ContentType.objects.get_for_model(RoomItem, for_concrete_model=False),
            )
            refresh_job_locked(locked_root)

        seal_job(root.id)
        with transaction.atomic():
            source = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=source.id)
            token = begin_batch_stage_locked(source, TELEGRAM_SCAN_STAGE)
            assert token
            fail_batch_stage_locked(
                source,
                stage=TELEGRAM_SCAN_STAGE,
                token=token,
                error="permanent source failure",
            )

        discarded = discard_failed_job(root.id)

    root.refresh_from_db()
    child.refresh_from_db()
    source.refresh_from_db()
    assert discarded.status == ProcessingJob.PARTIAL
    assert root.status == ProcessingJob.PARTIAL
    assert root.input_outcome == ProcessingJob.INPUT_PARTIAL
    assert root.failures_abandoned_at is not None
    assert source.status == ProcessingBatch.FAILED
    assert child.status == ProcessingJob.COMPLETED
    assert child.input_closed_at is not None
    assert child.input_outcome == ProcessingJob.INPUT_PARTIAL
    assert child.failed_batches == 0

    with pytest.raises(InvalidJobAction, match="cannot be retried"):
        retry_failed_job(root.id)


@pytest.mark.django_db(transaction=True)
def test_retry_failed_child_restarts_only_the_child_batch(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [message.id])
        seal_job(job.id)
        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.id)
            token = begin_batch_stage_locked(locked, NER_EXTRACT_STAGE)
            assert token
            fail_batch_stage_locked(
                locked,
                stage=NER_EXTRACT_STAGE,
                token=token,
                error="temporary NER failure",
            )

    job.refresh_from_db()
    assert job.status == ProcessingJob.WAITING_RETRY
    assert job.failed_batches == 1

    retry_failed_job(job.id)

    job.refresh_from_db()
    batch.refresh_from_db()
    assert job.status == ProcessingJob.RUNNING
    assert batch.status == ProcessingBatch.PENDING
    assert batch.current_stage == NER_EXTRACT_STAGE
    assert batch.failed_count == 0


@pytest.mark.django_db(transaction=True)
def test_sealed_child_waits_for_pending_handoffs_before_completing(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_scheduler.services.jobs.enqueue_handoff_dispatch"
    ), patch("app_scheduler.services.jobs.dispatch_handoffs"):
        root, _ = create_processing_job(
            processor=TELEGRAM_SCAN_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=[TELEGRAM_SCAN_STAGE],
            filters={},
            batch_size=1,
            max_inflight_batches=1,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        child, _ = create_processing_job(
            processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
            filters={},
            batch_size=10_000,
            max_inflight_batches=1,
            parent_job=root,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        with transaction.atomic():
            locked_root = ProcessingJob.objects.select_for_update().get(pk=root.id)
            source = create_batch_locked(
                locked_root,
                item_ids=[str(message.room_id)],
                item_content_type=ContentType.objects.get_for_model(RoomItem, for_concrete_model=False),
            )
            create_handoffs_locked(
                source,
                target_job=child,
                item_content_type=ContentType.objects.get_for_model(message, for_concrete_model=False),
                item_ids=[str(message.id)],
            )
            refresh_job_locked(locked_root)
        seal_job(root.id)
        with transaction.atomic():
            source = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=source.id)
            token = begin_batch_stage_locked(source, TELEGRAM_SCAN_STAGE)
            assert token
            complete_batch_stage_locked(source, stage=TELEGRAM_SCAN_STAGE, token=token)

    child.refresh_from_db()
    assert child.status == ProcessingJob.RUNNING
    assert ProcessingHandoff.objects.filter(target_job=child, status=ProcessingHandoff.PENDING).exists()

    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        dispatch_handoffs(child.id)
    assert ProcessingBatch.objects.filter(job=child).count() == 1


@pytest.mark.django_db(transaction=True)
def test_paused_child_keeps_scan_handoff_durable_until_resume(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_scheduler.services.jobs.enqueue_handoff_dispatch"
    ):
        root, _ = create_processing_job(
            processor=TELEGRAM_SCAN_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=[TELEGRAM_SCAN_STAGE],
            filters={},
            batch_size=1,
            max_inflight_batches=1,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        child, _ = create_processing_job(
            processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
            filters={},
            batch_size=10_000,
            max_inflight_batches=1,
            parent_job=root,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        pause_job(child.id)
        child.refresh_from_db()
        assert child.status == ProcessingJob.PAUSED

        with transaction.atomic():
            locked_root = ProcessingJob.objects.select_for_update().get(pk=root.id)
            source = create_batch_locked(
                locked_root,
                item_ids=[str(message.room_id)],
                item_content_type=ContentType.objects.get_for_model(RoomItem, for_concrete_model=False),
            )
            handoffs = create_handoffs_locked(
                source,
                target_job=child,
                item_content_type=ContentType.objects.get_for_model(message, for_concrete_model=False),
                item_ids=[str(message.id)],
            )

    assert len(handoffs) == 1
    assert ProcessingHandoff.objects.get(pk=handoffs[0].id).status == ProcessingHandoff.PENDING
    assert not ProcessingBatch.objects.filter(job=child).exists()


@pytest.mark.django_db(transaction=True)
def test_paused_job_stays_paused_when_an_inflight_batch_finishes(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [message.id])
        seal_job(job.id)
        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.id)
            token = begin_batch_stage_locked(locked, NER_EXTRACT_STAGE)
            assert token

        pause_job(job.id)

        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.id)
            complete_batch_stage_locked(
                locked,
                stage=NER_EXTRACT_STAGE,
                token=token,
            )

    job.refresh_from_db()
    assert job.status == ProcessingJob.PAUSED

    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        resume_job(job.id)

    job.refresh_from_db()
    assert job.status == ProcessingJob.RUNNING


@pytest.mark.django_db(transaction=True)
def test_cancel_root_settles_children_and_returns_cancelled(message_factory):
    message = message_factory(annotated_text="Rome", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        root, _ = create_processing_job(
            processor=TELEGRAM_SCAN_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=[TELEGRAM_SCAN_STAGE],
            filters={},
            batch_size=1,
            max_inflight_batches=1,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        child, _ = create_processing_job(
            processor=TELEGRAM_PROCESS_MESSAGES_PROCESSOR,
            mode=ProcessingJob.LIVE,
            workflow=TELEGRAM_PROCESS_MESSAGES_WORKFLOW,
            filters={},
            batch_size=10_000,
            max_inflight_batches=1,
            parent_job=root,
            fingerprint=uuid4().hex,
            deduplicate=False,
        )
        _create_message_batch(child, [message.id])

    child.refresh_from_db()
    assert child.status == ProcessingJob.COLLECTING
    cancelled = cancel_job(root.id)
    root.refresh_from_db()
    child.refresh_from_db()

    assert cancelled.status == ProcessingJob.CANCELLED
    assert root.status == ProcessingJob.CANCELLED
    assert child.status == ProcessingJob.CANCELLED


@pytest.mark.django_db(transaction=True)
def test_extract_task_persists_domain_payload_and_advances_to_store(message_factory):
    message = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [message.id])

    with patch("app_entity.tasks.extract_ner_data", return_value=([], {}, [message.id])), patch(
        "app_entity.tasks.build_ner_batch_metadata", return_value=_metadata("metadata-at-extraction")
    ), patch("app_scheduler.services.jobs.enqueue_batch_stage") as enqueue_stage:
        result = extract_ner_batch.run(batch.id)

    batch.refresh_from_db()
    payload = NERBatchResult.objects.get(batch=batch).payload
    assert result["status"] == "extracted"
    assert batch.current_stage == NER_STORE_STAGE
    assert payload["message_ids"] == [message.id.hex]
    assert batch.processor_version == "metadata-at-extraction"
    extraction_summary = batch.result_summary["stages"][NER_EXTRACT_STAGE]
    assert extraction_summary["inputs"][0]["count"] == 1
    assert extraction_summary["processed"][0]["count"] == 1
    assert extraction_summary["outputs"][0] == {
        "key": "extracted_entities",
        "count": 0,
        "kind": "entity",
        "unit": "entities",
    }
    assert "annotations" not in str(batch.result_summary).lower()
    assert enqueue_stage.call_args.args == (batch.id,)


def _create_metadata_batch(processor, stage, message):
    job, _ = create_processing_job(
        processor=processor,
        mode=ProcessingJob.BACKFILL,
        workflow=[stage],
        filters={"refresh": True},
        batch_size=1,
        max_inflight_batches=1,
        fingerprint=uuid4().hex,
        deduplicate=False,
    )
    return ProcessingBatch.objects.create(
        job=job,
        ordinal=1,
        item_ids=[str(message.pk)],
        item_count=1,
        current_stage=stage,
        processor_metadata={"slot": "default", "instruct": ""},
    )


@pytest.mark.django_db(transaction=True)
def test_embedding_task_persists_typed_stage_results(message_factory):
    message = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    batch = _create_metadata_batch(EMBEDDINGS_PROCESSOR, EMBEDDINGS_STAGE, message)

    with patch(
        "app_metadata.tasks.calculate_embeddings.run",
        return_value={"succeeded_ids": [str(message.pk)], "failed_ids": [], "skipped_ids": []},
    ), patch("app_scheduler.services.jobs.dispatch_ready_batches"), patch(
        "app_scheduler.services.jobs.dispatch_handoffs"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"):
        result = embed_processing_batch.run(batch.pk)

    batch.refresh_from_db()
    assert result["status"] == "embedded"
    stage_summary = batch.result_summary["stages"][EMBEDDINGS_STAGE]
    assert stage_summary["inputs"][0]["count"] == 1
    assert stage_summary["outputs"][0] == {
        "key": "embeddings_generated",
        "count": 1,
        "kind": "metadata.embedding",
        "unit": "embeddings",
    }


@pytest.mark.django_db(transaction=True)
def test_sentiment_task_persists_typed_stage_results(message_factory):
    message = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    batch = _create_metadata_batch(SENTIMENT_PROCESSOR, SENTIMENT_STAGE, message)

    with patch(
        "app_metadata.tasks.classify_sentiment.run",
        return_value={"succeeded_ids": [str(message.pk)], "failed_ids": [], "version": "model-v1"},
    ), patch("app_scheduler.services.jobs.dispatch_ready_batches"), patch(
        "app_scheduler.services.jobs.dispatch_handoffs"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"):
        result = sentiment_processing_batch.run(batch.pk)

    batch.refresh_from_db()
    assert result["status"] == "classified"
    stage_summary = batch.result_summary["stages"][SENTIMENT_STAGE]
    assert stage_summary["inputs"][0]["count"] == 1
    assert stage_summary["outputs"][0] == {
        "key": "sentiments_classified",
        "count": 1,
        "kind": "metadata.sentiment",
        "unit": "classifications",
    }
    assert stage_summary["metadata"]["model_version"] == "model-v1"


@pytest.mark.django_db(transaction=True)
def test_scan_child_skips_messages_without_ner_input_but_still_reaches_index(message_factory):
    processable = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    skipped = message_factory(
        room=processable.room,
        link="https://t.me/test_room/2",
        msg_id=2,
        text=None,
        annotated_text=None,
        stored_date=timezone.now(),
    )
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [processable.id, skipped.id])

    with patch(
        "app_entity.tasks.extract_ner_data",
        return_value=([{"name": "Madrid"}], {"entity-1": ["annotation-1"]}, [processable.id]),
    ) as extract, patch(
        "app_entity.tasks.build_ner_batch_metadata",
        return_value=_metadata("metadata-at-extraction"),
    ), patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        result = extract_ner_batch.run(batch.id)

    batch.refresh_from_db()
    payload = NERBatchResult.objects.get(batch=batch).payload
    assert result["status"] == "extracted"
    assert extract.call_args.args[0] == {
        "en": [{"pk": processable.id, "text": "Madrid"}],
    }
    assert payload["message_ids"] == [processable.id.hex]
    assert batch.processed_count == 1
    assert batch.skipped_count == 1
    assert not NERState.objects.filter(object_id=skipped.id).exists()
    assert batch.result_summary["stages"][NER_EXTRACT_STAGE]["outputs"][0]["count"] == 1
    assert "annotations" not in str(batch.result_summary).lower()

    with patch("app_entity.tasks.store_ner_results"), patch(
        "app_scheduler.services.jobs.enqueue_batch_stage"
    ):
        stored = store_ner_batch.run(batch.id)

    batch.refresh_from_db()
    assert stored["status"] == "stored"
    assert batch.current_stage == TELEGRAM_INDEX_STAGE
    store_inputs = batch.result_summary["stages"][NER_STORE_STAGE]["inputs"]
    assert store_inputs == [
        {"key": "messages", "count": 1, "kind": "telegram.message", "unit": "messages"},
        {"key": "extracted_entities", "count": 1, "kind": "entity", "unit": "entities"},
    ]
    assert "annotations" not in str(batch.result_summary).lower()

    with patch(
        "app_telegram.tasks.index_message_ids",
        return_value={"search_messages_indexed": 2, "analytics_entity_facts_synchronized": 1},
    ) as index_messages, patch(
        "app_scheduler.services.jobs.dispatch_ready_batches"
    ), patch("app_scheduler.services.jobs.dispatch_handoffs"), patch(
        "app_scheduler.services.jobs.enqueue_source_dispatch"
    ):
        indexed = index_messages_processing_batch.run(batch.id)

    batch.refresh_from_db()
    assert indexed["status"] == "indexed"
    index_messages.assert_called_once()
    assert batch.result_summary["stages"][TELEGRAM_INDEX_STAGE]["outputs"] == [
        {
            "key": "search_messages_indexed",
            "count": batch.item_count,
            "kind": "telegram.message",
            "unit": "messages",
        },
        {
            "key": "analytics_entity_facts_synchronized",
            "count": 1,
            "kind": "entity.fact",
            "unit": "pairs",
        },
    ]
    assert batch.result_summary["stages"][TELEGRAM_INDEX_STAGE]["metadata"]["search_indexing_enabled"] is True


@pytest.mark.django_db(transaction=True)
def test_scan_child_skips_unsupported_language_without_creating_ner_state(message_factory):
    message = message_factory(
        annotated_text="Texto no soportado",
        lang="fr",
        stored_date=timezone.now(),
    )
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [message.id])

    with patch("app_entity.tasks.extract_ner_data") as extract, patch(
        "app_entity.tasks.build_ner_batch_metadata",
        return_value=_metadata("metadata-at-extraction"),
    ), patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        result = extract_ner_batch.run(batch.id)

    batch.refresh_from_db()
    assert result["status"] == "extracted"
    assert not extract.called
    assert batch.processed_count == 0
    assert batch.skipped_count == 1
    assert batch.current_stage == NER_STORE_STAGE
    assert not NERState.objects.filter(object_id=message.id).exists()


@pytest.mark.django_db(transaction=True)
def test_store_deadlock_keeps_domain_payload_and_retries_only_storage(message_factory):
    message = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    job = _create_live_message_job()
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        batch = _create_message_batch(job, [message.id])
        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.id)
            claim_batch_messages_locked(locked)

    input_hash = build_ner_input_hash(message.annotated_text, message.lang, NER_INPUT_NORMALIZER_VERSION)
    payload = {
        "entities": [],
        "entity_to_ann": {},
        "message_ids": [message.id.hex],
        "input_hashes": {str(message.id): input_hash},
    }
    ProcessingBatch.objects.filter(pk=batch.id).update(
        current_stage=NER_STORE_STAGE,
        status=ProcessingBatch.PENDING,
        queued_at=timezone.now(),
    )
    NERBatchResult.objects.create(batch=batch, payload=payload)

    with patch("app_entity.tasks.store_ner_results", side_effect=OperationalError("deadlock detected")), patch.object(
        store_ner_batch, "retry", side_effect=Retry()
    ):
        with pytest.raises(Retry):
            store_ner_batch.run(batch.id)

    batch.refresh_from_db()
    assert batch.status == ProcessingBatch.RETRY_WAIT
    assert NERBatchResult.objects.get(batch=batch).payload == payload
    assert batch.stage_attempts[NER_STORE_STAGE] == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("backend", ["Elasticsearch", "ClickHouse"])
def test_index_failure_leaves_scan_batch_retryable(message_factory, backend):
    message = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [message.id])
        seal_job(job.id)
    ProcessingBatch.objects.filter(pk=batch.id).update(
        current_stage=TELEGRAM_INDEX_STAGE,
        status=ProcessingBatch.PENDING,
        queued_at=timezone.now(),
    )

    with patch(
        "app_telegram.tasks.index_message_ids", side_effect=RuntimeError(f"{backend} unavailable"),
    ), patch.object(
        index_messages_processing_batch, "retry", side_effect=Retry()
    ):
        with pytest.raises(Retry):
            index_messages_processing_batch.run(batch.id)

    batch.refresh_from_db()
    job.refresh_from_db()
    assert batch.status == ProcessingBatch.RETRY_WAIT
    assert batch.current_stage == TELEGRAM_INDEX_STAGE
    assert TELEGRAM_INDEX_STAGE not in batch.result_summary.get("stages", {})
    assert job.status != ProcessingJob.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_cancelled_claim_can_be_reclaimed_without_waiting_for_its_lease(message_factory):
    message = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"):
        first_job = _create_live_message_job()
        first_batch = _create_message_batch(first_job, [message.id])
        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=first_batch.id)
            claim_batch_messages_locked(locked)

        from app_scheduler.services.jobs import cancel_job

        cancel_job(first_job.id)
        second_job = _create_live_message_job()
        second_batch = _create_message_batch(second_job, [message.id])
        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=second_batch.id)
            claimed, skipped = claim_batch_messages_locked(locked)

    assert claimed == [message.id]
    assert skipped == []
    state = NERState.objects.get(
        content_type=ContentType.objects.get_for_model(message, for_concrete_model=False),
        object_id=message.id,
    )
    assert state.claimed_batch_id == second_batch.id


@pytest.mark.django_db
def test_completed_state_with_a_real_changed_hash_is_claimable(message_factory):
    message = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    state = NERState.objects.create(
        content_type=ContentType.objects.get_for_model(message, for_concrete_model=False),
        object_id=message.id,
        status=NERState.COMPLETED,
        processed_input_hash="old-hash",
    )
    input_hash = build_ner_input_hash(message.annotated_text, message.lang, NER_INPUT_NORMALIZER_VERSION)

    assert state_is_claimable(state, input_hash, timezone.now())
    state.processed_input_hash = ""
    assert not state_is_claimable(state, input_hash, timezone.now())


@pytest.mark.django_db
def test_scheduler_job_endpoints_are_read_control_only(api_client, staff_user):
    assert api_client.get("/api/v1/scheduler/processing-capacity").status_code in {401, 403}
    api_client.force_authenticate(user=staff_user)

    assert api_client.post("/api/v1/scheduler/jobs", {}, format="json").status_code == 405
    jobs = api_client.get("/api/v1/scheduler/jobs")
    assert jobs.status_code == 200
    assert jobs.data == {
        "next": None,
        "previous": None,
        "count": 0,
        "token": None,
        "results": [],
    }
    capacity = api_client.get("/api/v1/scheduler/processing-capacity")
    assert capacity.status_code == 200
    assert "active_jobs" in capacity.data
    assert Query.objects.count() == 0


@pytest.mark.django_db
def test_scheduler_job_list_uses_standard_paginated_response(api_client, staff_user):
    job = _create_live_message_job()
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(
        "/api/v1/scheduler/jobs",
        {"status": ProcessingJob.COLLECTING, "page": 1, "page-size": 1},
    )

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["token"] is None
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["id"] == str(job.pk)
    assert response.data["results"][0]["batches_url"].endswith(
        f"/api/v1/scheduler/jobs/{job.pk}/batches"
    )


@pytest.mark.django_db
def test_scheduler_job_action_is_audited_and_returns_standard_response(api_client, staff_user):
    job = _create_live_message_job()
    api_client.force_authenticate(user=staff_user)

    response = api_client.post(
        f"/api/v1/scheduler/jobs/{job.pk}/pause",
        {},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["result"]["status"] == ProcessingJob.PAUSED
    query = Query.objects.get()
    assert response.data["token"] == query.token.hex
    assert query.method == "POST"
    assert f"scheduler/jobs/{job.pk}/pause" in query.url


@pytest.mark.django_db(transaction=True)
def test_discard_failed_job_endpoint_returns_partial_job(api_client, staff_user, message_factory):
    message = message_factory(annotated_text="Madrid", stored_date=timezone.now())
    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_scheduler.services.jobs.enqueue_handoff_dispatch"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"), patch(
        "app_scheduler.services.jobs.dispatch_ready_batches"
    ), patch("app_scheduler.services.jobs.dispatch_handoffs"):
        job = _create_live_message_job()
        batch = _create_message_batch(job, [message.id])
        seal_job(job.id)
        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.id)
            token = begin_batch_stage_locked(locked, NER_EXTRACT_STAGE)
            assert token
            fail_batch_stage_locked(
                locked,
                stage=NER_EXTRACT_STAGE,
                token=token,
                error="manual discard test",
            )

        api_client.force_authenticate(user=staff_user)
        response = api_client.post(
            f"/api/v1/scheduler/jobs/{job.pk}/discard-failed",
            {},
            format="json",
        )

    assert response.status_code == 200
    assert response.data["result"]["status"] == ProcessingJob.PARTIAL
    assert response.data["result"]["input_outcome"] == ProcessingJob.INPUT_PARTIAL
    assert response.data["result"]["failures_abandoned_at"] is not None
