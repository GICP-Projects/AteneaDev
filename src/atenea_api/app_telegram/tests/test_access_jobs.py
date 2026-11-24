from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from app_scheduler.models import ProcessingBatch, ProcessingJob
from app_scheduler.services.jobs import create_handoffs_locked
from app_telegram.models import MessageItem, RoomItem, SeedItem
from app_telegram.services.jobs import (
    TELEGRAM_ACCESS_PROCESSOR,
    TELEGRAM_ACCESS_STAGE,
    create_access_job_from_queryset,
    create_populate_job_from_queryset,
    create_scan_jobs_from_queryset,
)
from app_telegram.services.pipelines import access_room_pipeline
from app_telegram.tasks import (
    access_rooms_processing_batch,
    index_rooms_from_seeds_processing_batch,
    populate_processing_batch,
    scan_rooms_processing_batch,
)


@pytest.mark.django_db
def test_access_job_balances_a_bounded_room_set_across_valid_auths(
    telegram_auth_factory,
    room_factory,
    settings,
):
    settings.TELEGRAM_ACCESS_ROOMS_PER_BATCH = 2
    settings.TELEGRAM_ACCESS_MAX_INFLIGHT = 2
    now = timezone.now()
    auths = [
        telegram_auth_factory(
            name=f"access-auth-{index}",
            api_id=91000 + index,
            api_hash=f"access-hash-{index}",
            wait_until=now - timedelta(minutes=1),
        )
        for index in range(2)
    ]
    telegram_auth_factory(
        name="access-auth-waiting",
        api_id=91002,
        api_hash="access-hash-waiting",
        wait_until=now + timedelta(hours=1),
    )
    telegram_auth_factory(
        name="access-auth-invalid",
        api_id=91003,
        api_hash="access-hash-invalid",
        is_valid=False,
        wait_until=now - timedelta(minutes=1),
    )

    eligible = [
        room_factory(
            tg_id=92000 + index,
            unique_name=f"orphan-{index}",
            link=f"https://t.me/orphan-{index}",
            is_valid=False,
        )
        for index in range(5)
    ]
    room_factory(
        tg_id=92010,
        unique_name="already-valid",
        link="https://t.me/already-valid",
        is_valid=True,
    )
    room_factory(
        tg_id=92011,
        unique_name="deleted-room",
        link="https://t.me/deleted-room",
        is_valid=False,
        is_deleted=True,
    )

    queryset = RoomItem.objects.filter(is_valid=False, is_deleted=False)
    expected_ids = list(queryset.order_by("id").values_list("id", flat=True)[:4])
    filters = {"room": [], "tags": [], "tag_match": "any", "lang": [], "is_channel": None}

    with patch("app_telegram.services.jobs.dispatch_ready_batches"):
        job, created = create_access_job_from_queryset(queryset, filters=filters)

    job.refresh_from_db()
    batches = list(ProcessingBatch.objects.filter(job=job).order_by("ordinal"))
    assert created is True
    assert job.processor == TELEGRAM_ACCESS_PROCESSOR
    assert job.workflow == [TELEGRAM_ACCESS_STAGE]
    assert job.mode == ProcessingJob.LIVE
    assert job.status == ProcessingJob.RUNNING
    assert job.total_items == 4
    assert [batch.item_count for batch in batches] == [2, 2]
    assert {room_id for batch in batches for room_id in batch.item_ids} == {str(room_id) for room_id in expected_ids}
    assert {batch.resource_key for batch in batches} == {f"telegram-auth:{auth.pk}" for auth in auths}
    assert all(batch.current_stage == TELEGRAM_ACCESS_STAGE for batch in batches)
    assert all(batch.recover_on_lease_expiry for batch in batches)


@pytest.mark.django_db
def test_access_pipeline_selects_only_followed_invalid_rooms(room_factory):
    eligible = room_factory(
        tg_id=92501,
        unique_name="orphan-to-recover",
        link="https://t.me/orphan-to-recover",
        tags=["recovery"],
        lang="es",
        is_channel=True,
        following=True,
        is_valid=False,
    )
    room_factory(
        tg_id=92502,
        unique_name="already-valid-for-access",
        link="https://t.me/already-valid-for-access",
        is_valid=True,
    )
    room_factory(
        tg_id=92503,
        unique_name="deleted-for-access",
        link="https://t.me/deleted-for-access",
        is_valid=False,
        is_deleted=True,
    )
    job = Mock(id="job-access", total_items=1)

    with patch(
        "app_telegram.services.pipelines.create_access_job_from_queryset",
        return_value=(job, True),
    ) as create_job:
        result = access_room_pipeline(
            room=["orphan-to-recover"],
            tags=["recovery"],
            lang=["es"],
            is_channel=True,
        )

    assert result == {"items_count": 1, "job_id": "job-access"}
    queryset = create_job.call_args.args[0]
    assert list(queryset.values_list("pk", flat=True)) == [eligible.pk]
    assert create_job.call_args.kwargs["filters"] == {
        "room": ["orphan-to-recover"],
        "tags": ["recovery"],
        "tag_match": "any",
        "lang": ["es"],
        "is_channel": True,
        "lastup_min": None,
        "lastup_max": None,
    }


@pytest.mark.django_db
def test_access_batch_records_resolved_deleted_and_deferred_rooms(
    telegram_auth_factory,
    room_factory,
    settings,
):
    settings.TELEGRAM_ACCESS_ROOMS_PER_BATCH = 3
    settings.TELEGRAM_ACCESS_MAX_INFLIGHT = 1
    auth = telegram_auth_factory(
        name="access-task-auth",
        api_id=93000,
        api_hash="access-task-hash",
        wait_until=timezone.now() - timedelta(minutes=1),
    )
    rooms = [
        room_factory(
            tg_id=93100 + index,
            unique_name=f"access-task-{index}",
            link=f"https://t.me/access-task-{index}",
            is_valid=False,
        )
        for index in range(3)
    ]

    with patch("app_telegram.services.jobs.dispatch_ready_batches"):
        job, _ = create_access_job_from_queryset(
            RoomItem.objects.filter(pk__in=[room.pk for room in rooms]),
            filters={},
        )
    batch = ProcessingBatch.objects.get(job=job)

    def resolve_partial(room_ids, auth_payload, model_class_name, token=None):
        assert auth_payload["pk"] == auth.pk
        assert model_class_name == "RoomItem"
        assert set(room_ids) == {room.pk for room in rooms}
        RoomItem.objects.filter(pk=rooms[0].pk).update(is_valid=True, access_auth=auth)
        RoomItem.objects.filter(pk=rooms[1].pk).update(is_deleted=True)

    with patch("app_telegram.tasks.access_entities.run", side_effect=resolve_partial):
        result = access_rooms_processing_batch.run(str(batch.pk))

    batch.refresh_from_db()
    job.refresh_from_db()
    assert result["status"] == "access_recalculated"
    assert batch.status == ProcessingBatch.COMPLETED
    assert batch.processed_count == 1
    assert batch.skipped_count == 1
    assert batch.deferred_count == 1
    access_summary = batch.result_summary["stages"]["telegram.access.rooms.resolve_hashes"]
    assert batch.result_summary["schema_version"] == 1
    assert access_summary["processed"][0]["key"] == "rooms_resolved"
    assert access_summary["processed"][0]["count"] == 1
    assert access_summary["inputs"][0]["count"] == len(rooms)
    assert access_summary["skipped"] == [{"key": "room_deleted", "count": 1}]
    assert job.status == ProcessingJob.COMPLETED
    assert job.processed_items == 1
    assert job.skipped_items == 1
    assert job.deferred_items == 1


@pytest.mark.django_db(transaction=True)
def test_scan_batch_records_scanned_rooms_and_handed_off_messages(
    telegram_auth_factory,
    room_factory,
    settings,
):
    settings.TELEGRAM_SCAN_ROOMS_PER_BATCH = 1
    auth = telegram_auth_factory(
        name="scan-summary-auth",
        api_id=94000,
        api_hash="scan-summary-hash",
        wait_until=timezone.now() - timedelta(minutes=1),
    )
    room = room_factory(
        tg_id=94100,
        unique_name="scan-summary-room",
        link="https://t.me/scan-summary-room",
        is_valid=True,
        access_auth=auth,
    )

    with patch("app_telegram.services.jobs.dispatch_ready_batches"):
        job, child, created = create_scan_jobs_from_queryset(
            RoomItem.objects.filter(pk=room.pk),
            filters={},
            options={"max_msgs": 20},
        )
    assert created
    batch = ProcessingBatch.objects.get(job=job)

    def scan_and_handoff(room_ids, _auth, **kwargs):
        assert room_ids == [str(room.pk)]
        assert kwargs["processing_handoff_target_job_id"] == str(child.pk)
        RoomItem.objects.filter(pk=room.pk).update(last_scan_at=timezone.now())
        with transaction.atomic():
            source = ProcessingBatch.objects.select_for_update().get(pk=batch.pk)
            target = ProcessingJob.objects.select_for_update().get(pk=child.pk)
            create_handoffs_locked(
                source,
                target_job=target,
                item_content_type=ContentType.objects.get_for_model(MessageItem, for_concrete_model=False),
                item_ids=[f"message-{index}" for index in range(3)],
            )
            source.processor_metadata = {
                **source.processor_metadata,
                "room_progress": {str(room.pk): {"state": "completed", "recovered_count": 3}},
            }
            source.save(update_fields=["processor_metadata"])

    with patch("app_telegram.tasks.scan_rooms.run", side_effect=scan_and_handoff), patch(
        "app_scheduler.services.jobs.dispatch_ready_batches"
    ), patch("app_scheduler.services.jobs.dispatch_handoffs"), patch(
        "app_scheduler.services.jobs.enqueue_source_dispatch"
    ), patch("app_scheduler.services.jobs.enqueue_handoff_dispatch"):
        result = scan_rooms_processing_batch.run(batch.pk)

    batch.refresh_from_db()
    scan_summary = batch.result_summary["stages"][job.workflow[0]]
    assert result["status"] == "scanned"
    assert scan_summary["inputs"][0]["count"] == 1
    assert scan_summary["processed"][0]["count"] == 1
    assert scan_summary["outputs"][0] == {
        "key": "messages_recovered",
        "count": 3,
        "kind": "telegram.message",
        "unit": "messages",
    }


@pytest.mark.django_db(transaction=True)
def test_populate_and_room_index_batches_record_stage_results(
    telegram_auth_factory,
    seed_factory,
    room_factory,
    settings,
):
    settings.TELEGRAM_POPULATE_SEEDS_PER_BATCH = 2
    telegram_auth_factory(
        name="populate-summary-auth",
        api_id=95000,
        api_hash="populate-summary-hash",
        wait_until=timezone.now() - timedelta(minutes=1),
    )
    valid_seed = seed_factory(link="https://t.me/populate-summary-valid")
    invalid_seed = seed_factory(link="https://t.me/populate-summary-invalid", is_valid=False)
    room = room_factory(
        tg_id=95100,
        unique_name="populate-summary-room",
        link="https://t.me/populate-summary-room",
        seed_item=valid_seed,
    )

    with patch("app_telegram.services.jobs.dispatch_ready_batches"):
        job, created = create_populate_job_from_queryset(
            SeedItem.objects.filter(pk__in=[valid_seed.pk, invalid_seed.pk]),
            filters={},
        )
    assert created
    batch = ProcessingBatch.objects.get(job=job)

    def populate(seed_ids, _auth, token=None, source_batch_id=None, lease_token=None):
        assert source_batch_id == batch.pk
        assert lease_token
        assert set(seed_ids) == {str(valid_seed.pk), str(invalid_seed.pk)}
        SeedItem.objects.filter(pk=valid_seed.pk).update(is_seeded=True)

    with patch("app_telegram.tasks.populate_seeds.run", side_effect=populate), patch(
        "app_scheduler.services.jobs.enqueue_batch_stage"
    ), patch("app_scheduler.services.jobs.dispatch_ready_batches"):
        populated = populate_processing_batch.run(batch.pk)

    batch.refresh_from_db()
    populate_summary = batch.result_summary["stages"][job.workflow[0]]
    assert populated["status"] == "populated"
    assert populate_summary["inputs"][0]["count"] == 2
    assert populate_summary["outputs"][0] == {
        "key": "seeds_populated",
        "count": 1,
        "kind": "telegram.seed",
        "unit": "seeds",
    }
    assert populate_summary["skipped"] == [{"key": "invalid_seed", "count": 1}]
    assert batch.current_stage == job.workflow[1]

    with patch("app_telegram.tasks.index_room_ids") as index_rooms, patch(
        "app_scheduler.services.jobs.dispatch_ready_batches"
    ), patch("app_scheduler.services.jobs.dispatch_handoffs"), patch(
        "app_scheduler.services.jobs.enqueue_source_dispatch"
    ):
        indexed = index_rooms_from_seeds_processing_batch.run(batch.pk)

    batch.refresh_from_db()
    room_summary = batch.result_summary["stages"][job.workflow[1]]
    assert indexed["status"] == "rooms_indexed"
    index_rooms.assert_called_once_with([room.pk])
    assert room_summary["inputs"][0]["count"] == 2
    assert room_summary["outputs"][0] == {
        "key": "rooms_submitted_for_indexing",
        "count": 1,
        "kind": "telegram.room",
        "unit": "rooms",
    }
