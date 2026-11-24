from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from pydantic import ValidationError

from app_dashboard.services.presentation import (
    _present_count_entries,
    aggregate_stage_summaries,
    result_metrics_for_summaries,
)
from app_scheduler.contracts import (
    ProcessingSummary,
    StageSummary,
    SummaryCount,
    SummaryReason,
    empty_processing_summary,
)
from app_scheduler.models import ProcessingBatch, ProcessingJob
from app_scheduler.serializers import ProcessingBatchSerializer
from app_scheduler.services.jobs import begin_batch_stage_locked, complete_batch_stage_locked

def test_processing_summary_accepts_null_and_measured_zero_counts():
    summary = ProcessingSummary.model_validate(
        {
            "schema_version": 1,
            "stages": {
                "pipeline.stage": {
                    "inputs": [
                        {"key": "unknown", "count": None, "kind": "record"},
                        {"key": "measured", "count": 0, "kind": "record"},
                    ],
                    "skipped": [{"key": "no_match", "count": None}],
                }
            },
        }
    )

    assert summary.stages["pipeline.stage"].inputs[0].count is None
    assert summary.stages["pipeline.stage"].inputs[1].count == 0
    assert summary.stages["pipeline.stage"].skipped[0].count is None


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "stages": {}},
        {
            "schema_version": 1,
            "stages": {"pipeline.stage": {"outputs": [{"key": "items", "count": -1, "kind": "record"}]}},
        },
        {
            "schema_version": 1,
            "stages": {"pipeline.stage": {"inputs": [{"key": "items", "count": True, "kind": "record"}]}},
        },
    ],
)
def test_processing_summary_rejects_unknown_version_and_invalid_counts(payload):
    with pytest.raises(ValidationError):
        ProcessingSummary.model_validate(payload)


def test_result_summary_field_default_and_django_validator_use_contract():
    field = ProcessingBatch._meta.get_field("result_summary")
    assert field.get_default() == empty_processing_summary()
    assert field.get_default() is not field.get_default()

    with pytest.raises(DjangoValidationError):
        field.run_validators(
            {"schema_version": 1, "stages": {"x": {"inputs": [{"key": "a", "count": -2, "kind": "x"}]}}}
        )


def test_processing_batch_serializer_validates_writable_result_summary():
    valid = {
        "schema_version": 1,
        "stages": {"pipeline.stage": {"outputs": [{"key": "items", "count": None, "kind": "record"}]}},
    }
    serializer = ProcessingBatchSerializer(data={"result_summary": valid}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["result_summary"] == ProcessingSummary.model_validate(valid).model_dump(mode="json")

    invalid = ProcessingBatchSerializer(
        data={"result_summary": {"schema_version": 9, "stages": {}}},
        partial=True,
    )
    assert not invalid.is_valid()
    assert "result_summary" in invalid.errors


@pytest.mark.django_db(transaction=True)
def test_stage_helper_persists_typed_summary_and_rejects_invalid_existing_payload():
    stage = "test.summary.stage"
    job = ProcessingJob.objects.create(
        processor="test.summary",
        mode=ProcessingJob.LIVE,
        workflow=[stage],
        fingerprint=uuid4().hex,
        batch_size=1,
        max_inflight_batches=1,
        status=ProcessingJob.RUNNING,
    )
    batch = ProcessingBatch.objects.create(
        job=job,
        ordinal=1,
        item_ids=["test-item"],
        item_count=1,
        current_stage=stage,
    )
    stage_summary = StageSummary(
        inputs=[SummaryCount(key="records", count=1, kind="record")],
        outputs=[SummaryCount(key="processed", count=0, kind="record")],
        skipped=[SummaryReason(key="not_ready", count=None)],
    )

    with _patch_job_callbacks():
        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=batch.pk)
            token = begin_batch_stage_locked(locked, stage)
            complete_batch_stage_locked(locked, stage=stage, token=token, stage_summary=stage_summary)

    batch.refresh_from_db()
    assert batch.result_summary == {
        "schema_version": 1,
        "stages": {stage: stage_summary.model_dump(mode="json")},
    }

    second = ProcessingBatch.objects.create(
        job=job,
        ordinal=2,
        item_ids=["another-item"],
        item_count=1,
        current_stage=stage,
        result_summary={"schema_version": 7, "stages": {}},
    )
    with _patch_job_callbacks(), pytest.raises(ValidationError):
        with transaction.atomic():
            locked = ProcessingBatch.objects.select_for_update().select_related("job").get(pk=second.pk)
            token = begin_batch_stage_locked(locked, stage)
            complete_batch_stage_locked(locked, stage=stage, token=token, stage_summary=stage_summary)


class _patch_job_callbacks:
    """Patch post-commit dispatchers while exercising real database writes."""

    def __enter__(self):
        from unittest.mock import patch

        self.stack = patch("app_scheduler.services.jobs.dispatch_ready_batches")
        self.stack.start()
        self.handoffs = patch("app_scheduler.services.jobs.dispatch_handoffs")
        self.handoffs.start()
        self.source = patch("app_scheduler.services.jobs.enqueue_source_dispatch")
        self.source.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.source.stop()
        self.handoffs.stop()
        self.stack.stop()


def test_dashboard_ignores_legacy_summary_and_keeps_sequential_outputs_separate():
    assert result_metrics_for_summaries([{"entities_stored": 5, "annotations_stored": 8}]) == []

    summary = {
        "schema_version": 1,
        "stages": {
            "entity.ner.extract": {
                "outputs": [{"key": "items", "count": 5, "kind": "record"}]
            },
            "entity.ner.store": {
                "outputs": [{"key": "items", "count": 5, "kind": "record"}]
            },
        },
    }
    metrics = result_metrics_for_summaries([summary])
    assert [(item["stage_key"], item["count"]) for item in metrics] == [
        ("entity.ner.extract", 5),
        ("entity.ner.store", 5),
    ]
    presented = _present_count_entries(
        ProcessingSummary.model_validate(
            {"schema_version": 1, "stages": {"stage": {"inputs": [{"key": "unknown", "count": None, "kind": "record"}]}}}
        ).stages["stage"].inputs
    )
    assert presented[0]["value"] == "—"

    batch = SimpleNamespace(job=SimpleNamespace(workflow=["entity.ner.extract"]), result_summary=summary)
    assert aggregate_stage_summaries([batch])[0]["outputs"][0]["count"] == 5
