from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from app_scheduler.models import ProcessingBatch, ProcessingHandoff, ProcessingJob
from app_scheduler.services.jobs import (
    InvalidJobAction,
    begin_batch_stage_locked,
    complete_batch_stage_locked,
    create_batch_locked,
    create_handoffs_locked,
    create_processing_job,
    discard_failed_job,
    dispatch_handoffs,
    fail_batch_stage_locked,
    refresh_processing_job,
    refresh_job_locked,
    retry_failed_job,
    seal_job,
)


SOURCE_STAGE = "scan.collect"
NER_EXTRACT_STAGE = "ner.extract"
NER_STORE_STAGE = "ner.store"
INDEX_STAGE = "index.write"


@pytest.fixture
def scheduler_publication():
    """Keep the test focused on durable state, not on a live Celery broker."""

    with patch("app_scheduler.services.jobs.enqueue_batch_stage"), patch(
        "app_scheduler.services.jobs.enqueue_handoff_dispatch"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"):
        yield


def _item_type():
    return ContentType.objects.get_for_model(
        ProcessingJob,
        for_concrete_model=False,
    )


def _job(processor, workflow, parent=None):
    job, _ = create_processing_job(
        processor=processor,
        mode=ProcessingJob.LIVE,
        workflow=workflow,
        filters={"case": processor},
        batch_size=2,
        max_inflight_batches=2,
        parent_job=parent,
        fingerprint=uuid4().hex,
        deduplicate=False,
    )
    return job


def _batch(job, item_ids):
    with transaction.atomic():
        locked_job = ProcessingJob.objects.select_for_update().get(pk=job.pk)
        batch = create_batch_locked(
            locked_job,
            item_ids=item_ids,
            item_content_type=_item_type(),
            schedule_dispatch=False,
        )
        refresh_job_locked(locked_job)
    return batch


def _materialize(handoff):
    dispatch_handoffs(handoff.target_job_id)
    handoff.refresh_from_db()
    assert handoff.target_batch_id is not None
    return ProcessingBatch.objects.get(pk=handoff.target_batch_id)


def _complete(batch_id, stage, *, output_job=None, output_ids=None):
    handoff_id = None
    with transaction.atomic():
        batch = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch_id)
        token = begin_batch_stage_locked(batch, stage)
        assert token, f"Batch {batch_id} was not runnable at {stage}"
        if output_job is not None:
            target = ProcessingJob.objects.select_for_update().get(pk=output_job.pk)
            handoff_id = create_handoffs_locked(
                batch,
                target_job=target,
                item_content_type=_item_type(),
                item_ids=output_ids or [],
            )[0].pk
        complete_batch_stage_locked(batch, stage=stage, token=token)
    return handoff_id


def _fail(batch_id, stage, message):
    with transaction.atomic():
        batch = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch_id)
        token = begin_batch_stage_locked(batch, stage)
        assert token, f"Batch {batch_id} was not runnable at {stage}"
        fail_batch_stage_locked(batch, stage=stage, token=token, error=message)


def _complete_ner_batch(batch, index_job, label):
    _complete(batch.pk, NER_EXTRACT_STAGE)
    handoff_id = _complete(
        batch.pk,
        NER_STORE_STAGE,
        output_job=index_job,
        output_ids=[f"indexed-{label}"],
    )
    return _materialize(ProcessingHandoff.objects.get(pk=handoff_id))


def _complete_index_batch(batch):
    _complete(batch.pk, INDEX_STAGE)


def _refresh_all(*jobs):
    for job in jobs:
        refresh_processing_job(job.pk)


@pytest.mark.django_db(transaction=True)
def test_staff_api_can_retry_descendant_from_completed_parent(api_client, staff_user, scheduler_publication):
    parent = _job("pipeline.scan", [SOURCE_STAGE])
    child = _job("pipeline.ner", [NER_EXTRACT_STAGE, NER_STORE_STAGE], parent=parent)
    source = _batch(parent, ["room"])
    seal_job(parent.pk)
    handoff_id = _complete(source.pk, SOURCE_STAGE, output_job=child, output_ids=["message"])
    batch = _materialize(ProcessingHandoff.objects.get(pk=handoff_id))
    _complete(batch.pk, NER_EXTRACT_STAGE)
    _fail(batch.pk, NER_STORE_STAGE, "temporary database failure")
    url = f"/api/v1/scheduler/jobs/{parent.pk}/retry-pending"
    assert api_client.post(url).status_code in {401, 403}
    api_client.force_authenticate(staff_user)
    response = api_client.post(url)
    assert response.status_code == 200
    assert response.data["result"]["status"] == ProcessingJob.COMPLETED
    batch.refresh_from_db()
    assert batch.status == ProcessingBatch.PENDING
    assert batch.current_stage == NER_STORE_STAGE
    assert batch.completed_stages == [NER_EXTRACT_STAGE]


@pytest.mark.django_db(transaction=True)
def test_three_job_pipeline_waits_for_parent_retry_and_drains_different_workflows(
    scheduler_publication,
):
    # Verifies that a failed parent batch pauses the three-Job chain and that retrying it drains NER and INDEX.
    parent = _job("pipeline.scan", [SOURCE_STAGE])
    ner = _job("pipeline.ner", [NER_EXTRACT_STAGE, NER_STORE_STAGE], parent=parent)
    index = _job("pipeline.index", [INDEX_STAGE], parent=ner)
    first_source = _batch(parent, ["room-1"])
    second_source = _batch(parent, ["room-2"])
    seal_job(parent.pk)

    first_handoff_id = _complete(
        first_source.pk,
        SOURCE_STAGE,
        output_job=ner,
        output_ids=["message-1"],
    )
    first_handoff = ProcessingHandoff.objects.get(pk=first_handoff_id)
    ner_batch_1 = _materialize(first_handoff)
    index_batch_1 = _complete_ner_batch(ner_batch_1, index, "message-1")
    _complete_index_batch(index_batch_1)

    _fail(second_source.pk, SOURCE_STAGE, "temporary scan failure")

    parent.refresh_from_db()
    ner.refresh_from_db()
    index.refresh_from_db()
    assert parent.status == ProcessingJob.WAITING_RETRY
    assert ner.status in {ProcessingJob.COLLECTING, ProcessingJob.RUNNING}
    assert index.status in {ProcessingJob.COLLECTING, ProcessingJob.RUNNING}
    assert ner.input_closed_at is None
    assert index.input_closed_at is None

    retry_failed_job(parent.pk)
    parent.refresh_from_db()
    assert parent.status == ProcessingJob.RUNNING
    assert ProcessingBatch.objects.get(pk=second_source.pk).status == ProcessingBatch.PENDING

    second_handoff_id = _complete(
        second_source.pk,
        SOURCE_STAGE,
        output_job=ner,
        output_ids=["message-2"],
    )
    second_handoff = ProcessingHandoff.objects.get(pk=second_handoff_id)
    ner_batch_2 = _materialize(second_handoff)
    index_batch_2 = _complete_ner_batch(ner_batch_2, index, "message-2")
    _complete_index_batch(index_batch_2)
    _refresh_all(index, ner, parent)

    parent.refresh_from_db()
    ner.refresh_from_db()
    index.refresh_from_db()
    assert parent.status == ProcessingJob.COMPLETED
    assert ner.status == ProcessingJob.COMPLETED
    assert index.status == ProcessingJob.COMPLETED
    assert parent.failed_batches == 0
    assert ner.total_batches == 2
    assert index.total_batches == 2


@pytest.mark.django_db(transaction=True)
def test_child_failure_waits_and_retrying_child_does_not_restart_parent(
    scheduler_publication,
):
    # Verifies that a child batch can be retried in place while the completed parent batch is preserved.
    parent = _job("pipeline.scan", [SOURCE_STAGE])
    ner = _job("pipeline.ner", [NER_EXTRACT_STAGE, NER_STORE_STAGE], parent=parent)
    index = _job("pipeline.index", [INDEX_STAGE], parent=ner)
    source = _batch(parent, ["room-1"])
    seal_job(parent.pk)

    with transaction.atomic():
        source = ProcessingBatch.objects.select_for_update().get(pk=source.pk)
        target = ProcessingJob.objects.select_for_update().get(pk=ner.pk)
        handoff = create_handoffs_locked(
            source,
            target_job=target,
            item_content_type=_item_type(),
            item_ids=["message-1"],
        )[0]
        token = begin_batch_stage_locked(source, SOURCE_STAGE)
        assert token
        complete_batch_stage_locked(source, stage=SOURCE_STAGE, token=token)

    ner_batch = _materialize(handoff)
    _fail(ner_batch.pk, NER_EXTRACT_STAGE, "temporary NER failure")

    parent.refresh_from_db()
    ner.refresh_from_db()
    assert ner.status == ProcessingJob.WAITING_RETRY
    assert parent.status == ProcessingJob.COMPLETED
    assert parent.completed_batches == 1
    assert parent.failed_batches == 0

    retry_failed_job(ner.pk)
    parent.refresh_from_db()
    ner.refresh_from_db()
    assert parent.status == ProcessingJob.COMPLETED
    assert ner.status == ProcessingJob.RUNNING
    assert ProcessingBatch.objects.get(pk=source.pk).status == ProcessingBatch.COMPLETED

    index_batch = _complete_ner_batch(ner_batch, index, "message-1")
    _complete_index_batch(index_batch)
    _refresh_all(index, ner, parent)

    parent.refresh_from_db()
    ner.refresh_from_db()
    index.refresh_from_db()
    assert parent.status == ProcessingJob.COMPLETED
    assert ner.status == ProcessingJob.COMPLETED
    assert index.status == ProcessingJob.COMPLETED
    assert ner.failed_batches == 0


@pytest.mark.django_db(transaction=True)
def test_discarding_parent_failure_completes_received_descendants_as_partial_input(
    scheduler_publication,
):
    # Verifies that discarding a parent failure yields PARTIAL upstream and COMPLETED descendants with partial input.
    parent = _job("pipeline.scan", [SOURCE_STAGE])
    ner = _job("pipeline.ner", [NER_EXTRACT_STAGE, NER_STORE_STAGE], parent=parent)
    index = _job("pipeline.index", [INDEX_STAGE], parent=ner)
    good_source = _batch(parent, ["room-good"])
    failed_source = _batch(parent, ["room-failed"])
    seal_job(parent.pk)

    with transaction.atomic():
        source = ProcessingBatch.objects.select_for_update().get(pk=good_source.pk)
        target = ProcessingJob.objects.select_for_update().get(pk=ner.pk)
        handoff = create_handoffs_locked(
            source,
            target_job=target,
            item_content_type=_item_type(),
            item_ids=["message-good"],
        )[0]
        token = begin_batch_stage_locked(source, SOURCE_STAGE)
        assert token
        complete_batch_stage_locked(source, stage=SOURCE_STAGE, token=token)

    ner_batch = _materialize(handoff)
    index_batch = _complete_ner_batch(ner_batch, index, "message-good")
    _complete_index_batch(index_batch)
    _fail(failed_source.pk, SOURCE_STAGE, "permanent scan failure")

    parent.refresh_from_db()
    assert parent.status == ProcessingJob.WAITING_RETRY

    discarded = discard_failed_job(parent.pk)
    parent.refresh_from_db()
    ner.refresh_from_db()
    index.refresh_from_db()
    failed_source.refresh_from_db()

    assert discarded.status == ProcessingJob.PARTIAL
    assert parent.status == ProcessingJob.PARTIAL
    assert parent.input_outcome == ProcessingJob.INPUT_PARTIAL
    assert failed_source.status == ProcessingBatch.FAILED
    assert ner.status == ProcessingJob.COMPLETED
    assert ner.input_outcome == ProcessingJob.INPUT_PARTIAL
    assert index.status == ProcessingJob.COMPLETED
    assert index.input_outcome == ProcessingJob.INPUT_PARTIAL

    with pytest.raises(InvalidJobAction, match="cannot be retried"):
        retry_failed_job(parent.pk)


@pytest.mark.django_db(transaction=True)
def test_parent_and_child_failures_can_be_retried_independently(
    scheduler_publication,
):
    # Verifies that independent parent and child failures recover separately without replaying successful stages.
    parent = _job("pipeline.scan", [SOURCE_STAGE])
    ner = _job("pipeline.ner", [NER_EXTRACT_STAGE, NER_STORE_STAGE], parent=parent)
    index = _job("pipeline.index", [INDEX_STAGE], parent=ner)
    good_source = _batch(parent, ["room-good"])
    failed_source = _batch(parent, ["room-failed"])
    seal_job(parent.pk)

    with transaction.atomic():
        source = ProcessingBatch.objects.select_for_update().get(pk=good_source.pk)
        target = ProcessingJob.objects.select_for_update().get(pk=ner.pk)
        handoff = create_handoffs_locked(
            source,
            target_job=target,
            item_content_type=_item_type(),
            item_ids=["message-good"],
        )[0]
        token = begin_batch_stage_locked(source, SOURCE_STAGE)
        assert token
        complete_batch_stage_locked(source, stage=SOURCE_STAGE, token=token)

    ner_batch_1 = _materialize(handoff)
    _fail(ner_batch_1.pk, NER_EXTRACT_STAGE, "temporary NER failure")
    _fail(failed_source.pk, SOURCE_STAGE, "temporary scan failure")

    parent.refresh_from_db()
    ner.refresh_from_db()
    assert parent.status == ProcessingJob.WAITING_RETRY
    assert ner.status == ProcessingJob.WAITING_RETRY

    # The child can recover its own failed batch while the parent remains
    # blocked on a different failed source batch.
    retry_failed_job(ner.pk)
    index_batch_1 = _complete_ner_batch(ner_batch_1, index, "message-good")
    _complete_index_batch(index_batch_1)
    parent.refresh_from_db()
    assert parent.status == ProcessingJob.WAITING_RETRY
    assert ProcessingBatch.objects.get(pk=failed_source.pk).status == ProcessingBatch.FAILED

    # Retrying the parent later sends the missing input into the already
    # recovered child; neither completed stage is repeated.
    retry_failed_job(parent.pk)
    second_handoff_id = _complete(
        failed_source.pk,
        SOURCE_STAGE,
        output_job=ner,
        output_ids=["message-recovered"],
    )
    second_handoff = ProcessingHandoff.objects.get(pk=second_handoff_id)
    ner_batch_2 = _materialize(second_handoff)
    index_batch_2 = _complete_ner_batch(ner_batch_2, index, "message-recovered")
    _complete_index_batch(index_batch_2)
    _refresh_all(index, ner, parent)

    parent.refresh_from_db()
    ner.refresh_from_db()
    index.refresh_from_db()
    assert parent.status == ProcessingJob.COMPLETED
    assert ner.status == ProcessingJob.COMPLETED
    assert index.status == ProcessingJob.COMPLETED
    assert ner.total_batches == 2
    assert index.total_batches == 2
