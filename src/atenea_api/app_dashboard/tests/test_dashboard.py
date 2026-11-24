from uuid import UUID, uuid4
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django_celery_beat.models import PeriodicTask

from app_dashboard.forms import JobFilterForm, JobLaunchForm, ScheduleForm, SplitListField
from app_dashboard.services.graph import build_job_graph, build_job_pipeline, build_job_pipeline_updates
from app_scheduler.models import ProcessingBatch, ProcessingHandoff, ProcessingJob
from app_scheduler.serializers import ProcessingTriggerSerializer


def make_job(**overrides):
    values = {
        "processor": "telegram.scan.rooms",
        "mode": ProcessingJob.BACKFILL,
        "workflow": [
            "telegram.scan.rooms.collect_messages",
            "entity.ner.extract",
            "entity.ner.store",
        ],
        "fingerprint": uuid4().hex,
        "status": ProcessingJob.RUNNING,
        "total_batches": 3,
        "estimated_batches_total": 3,
    }
    values.update(overrides)
    return ProcessingJob.objects.create(**values)


@pytest.mark.django_db
@pytest.mark.parametrize("status,failed,recovered", [
    (ProcessingJob.COMPLETED, 0, True),
    (ProcessingJob.COMPLETED, 1, False),
    (ProcessingJob.RUNNING, 0, False),
    (ProcessingJob.WAITING_RETRY, 1, False),
    (ProcessingJob.FAILED, 0, False),
    (ProcessingJob.PARTIAL, 1, False),
])
def test_live_error_distinguishes_recovered_history_without_erasing_it(staff_user, status, failed, recovered):
    root = make_job(status=status, failed_batches=failed, last_error="previous database deadlock")
    make_job(processor="telegram.process_messages", parent_job=root, status=ProcessingJob.WAITING_RETRY, failed_batches=1)
    client = Client()
    client.force_login(staff_user)
    response = client.get(reverse("app_dashboard:job-live", args=[root.pk]))
    content = response.content.decode()
    assert ("Last recovered error:" in content) is recovered
    assert ("alert alert-error" in content) is not recovered
    assert "Downstream processing is still pending" in content
    root.refresh_from_db()
    assert root.last_error == "previous database deadlock"


@pytest.mark.django_db
def test_completed_parent_exposes_child_retry_in_live_fragment_and_status_filter(staff_user):
    parent = make_job(status=ProcessingJob.COMPLETED)
    child = make_job(
        processor="telegram.process_messages", parent_job=parent,
        status=ProcessingJob.WAITING_RETRY, failed_batches=1,
    )
    client = Client()
    client.force_login(staff_user)
    response = client.get(reverse("app_dashboard:job-live", args=[parent.pk]))
    assert "Downstream processing is still pending" in response.content.decode()
    assert reverse("app_dashboard:job-action", args=[parent.pk, "retry-pending"]) in response.content.decode()
    listed = client.get(reverse("app_dashboard:jobs"), {"status": "waiting_retry"})
    assert parent.pk in [job["object"].pk for job in listed.context["jobs"]]
    assert listed.context["retryable_message_jobs"] == 1


@pytest.mark.django_db
def test_bulk_message_retry_is_staff_post_only_and_keeps_index_progress(staff_user):
    parent = make_job(status=ProcessingJob.COMPLETED)
    child = make_job(
        processor="telegram.process_messages", parent_job=parent,
        workflow=["entity.ner.extract", "entity.ner.store", "telegram.index.messages.write"],
        status=ProcessingJob.WAITING_RETRY, failed_batches=1,
    )
    batch = ProcessingBatch.objects.create(
        job=child, ordinal=1, status=ProcessingBatch.FAILED, item_count=1, failed_count=1,
        current_stage="telegram.index.messages.write", completed_stages=["entity.ner.extract", "entity.ner.store"],
    )
    abandoned = make_job(
        processor="telegram.process_messages", status=ProcessingJob.PARTIAL,
        failed_batches=1, failures_abandoned_at=parse_datetime("2026-10-01T00:00:00Z"),
    )
    other = make_job(processor="metadata.sentiment", status=ProcessingJob.WAITING_RETRY, failed_batches=1)
    paused = make_job(processor="telegram.process_messages", status=ProcessingJob.PAUSED, failed_batches=1)
    url = reverse("app_dashboard:retry-message-processing")
    client = Client()
    assert client.post(url).status_code == 302
    client.force_login(staff_user)
    assert client.get(url).status_code == 405
    with patch("app_scheduler.services.jobs.dispatch_ready_batches"), patch(
        "app_scheduler.services.jobs.dispatch_handoffs"
    ), patch("app_scheduler.services.jobs.enqueue_source_dispatch"):
        response = client.post(url)
    assert response.status_code == 302
    batch.refresh_from_db()
    parent.refresh_from_db()
    abandoned.refresh_from_db()
    other.refresh_from_db()
    paused.refresh_from_db()
    assert batch.status == ProcessingBatch.PENDING
    assert batch.current_stage == "telegram.index.messages.write"
    assert batch.completed_stages == ["entity.ner.extract", "entity.ner.store"]
    assert parent.status == ProcessingJob.COMPLETED
    assert abandoned.status == ProcessingJob.PARTIAL
    assert other.status == ProcessingJob.WAITING_RETRY
    assert paused.status == ProcessingJob.PAUSED
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(staff_user)
    assert csrf_client.post(url).status_code == 403


def _index_summary(*, search_enabled=True):
    return {"schema_version": 1, "stages": {"telegram.index.messages.write": {
        "inputs": [{"key": "messages", "count": 3, "kind": "telegram.message", "unit": "messages"}],
        "outputs": [
            {"key": "search_messages_indexed", "count": 3 if search_enabled else 0,
             "kind": "telegram.message", "unit": "messages"},
            {"key": "analytics_entity_facts_synchronized", "count": 5,
             "kind": "entity.fact", "unit": "pairs"},
        ],
        "metadata": {"search_indexing_enabled": search_enabled},
    }}}


@pytest.mark.django_db
@pytest.mark.parametrize("search_enabled", [True, False])
def test_index_graph_shows_both_destinations_and_search_omission(staff_user, search_enabled):
    stage_key = "telegram.index.messages.write"
    job = make_job(processor="telegram.index.messages", workflow=[stage_key], status=ProcessingJob.COMPLETED)
    batch = ProcessingBatch.objects.create(
        job=job, ordinal=1, status=ProcessingBatch.COMPLETED, item_count=3,
        completed_stages=[stage_key], result_summary=_index_summary(search_enabled=search_enabled),
    )
    graph = build_job_graph(job)
    stage = graph["workflow_summary"][0]
    metrics = {entry["destination"]: entry for entry in stage["outputs"]}
    assert metrics["Elasticsearch"]["count"] == (3 if search_enabled else 0)
    assert metrics["ClickHouse"]["count"] == 5
    assert not any(entry["partial"] for entry in stage["outputs"])
    assert ("skipped by configuration" in stage["notice"]) is (not search_enabled)
    client = Client()
    client.force_login(staff_user)
    response = client.get(reverse("app_dashboard:job-detail", args=[job.pk]))
    assert response.status_code == 200
    body = response.content.decode()
    assert ">Elasticsearch</span>" in body
    assert ">ClickHouse</span>" in body
    if not search_enabled:
        assert "Search indexing skipped by configuration." in body
    batch.refresh_from_db()
    assert batch.result_summary == _index_summary(search_enabled=search_enabled)


@pytest.mark.django_db
@pytest.mark.parametrize("has_record", [True, False])
def test_completed_legacy_index_keeps_its_original_counts(staff_user, has_record):
    stage_key = "telegram.index.messages.write"
    legacy = {"schema_version": 1, "stages": {stage_key: {"outputs": [
        {"key": "messages_submitted_for_indexing", "count": 7, "kind": "telegram.message", "unit": "messages"},
    ]}} if has_record else {}}
    job = make_job(processor="telegram.index.messages", workflow=[stage_key], status=ProcessingJob.COMPLETED)
    batch = ProcessingBatch.objects.create(
        job=job, ordinal=1, status=ProcessingBatch.COMPLETED, item_count=7,
        completed_stages=[stage_key], result_summary=legacy,
    )
    stage = build_job_graph(job)["workflow_summary"][0]
    assert stage["notice"] == ""
    assert [metric["count"] for metric in stage["outputs"]] == ([7] if has_record else [])
    assert all(not metric["destination"] for metric in stage["outputs"])
    client = Client()
    client.force_login(staff_user)
    body = client.get(reverse("app_dashboard:job-detail", args=[job.pk])).content.decode()
    assert "Breakdown by destination not recorded." not in body
    batch.refresh_from_db()
    assert batch.result_summary == legacy


@pytest.mark.django_db
@pytest.mark.parametrize("has_legacy_record", [True, False])
def test_mixed_index_summaries_mark_destination_counts_as_partial(has_legacy_record):
    from app_dashboard.services.presentation import result_metrics_for_batches

    stage_key = "telegram.index.messages.write"
    job = make_job(processor="telegram.index.messages", workflow=[stage_key], status=ProcessingJob.COMPLETED)
    old = ProcessingBatch.objects.create(
        job=job, ordinal=1, status=ProcessingBatch.COMPLETED, completed_stages=[stage_key],
        result_summary={"schema_version": 1, "stages": {stage_key: {"outputs": [
            {"key": "messages_submitted_for_indexing", "count": 7, "kind": "telegram.message", "unit": "messages"},
        ]}} if has_legacy_record else {}},
    )
    new = ProcessingBatch.objects.create(
        job=job, ordinal=2, status=ProcessingBatch.COMPLETED, completed_stages=[stage_key],
        result_summary=_index_summary(),
    )
    stage = build_job_graph(job)["workflow_summary"][0]
    assert stage["notice"].startswith("Partial destination breakdown:")
    expected = {
        "search_messages_indexed": 3,
        "analytics_entity_facts_synchronized": 5,
    }
    if has_legacy_record:
        expected["messages_submitted_for_indexing"] = 7
    assert {entry["key"]: entry["count"] for entry in stage["outputs"]} == expected
    for metrics in (stage["outputs"], result_metrics_for_batches([old, new])):
        assert all(entry["partial"] for entry in metrics)
        assert all(entry["label"].endswith("(partial)") for entry in metrics)


@pytest.mark.django_db
def test_pending_index_does_not_claim_historical_details_are_missing():
    stage_key = "telegram.index.messages.write"
    job = make_job(processor="telegram.index.messages", workflow=[stage_key])
    ProcessingBatch.objects.create(job=job, ordinal=1, status=ProcessingBatch.PENDING)
    assert build_job_graph(job)["workflow_summary"][0]["notice"] == ""


@pytest.mark.django_db
def test_dashboard_requires_staff_and_preserves_next(staff_user):
    client = Client()
    response = client.get(reverse("app_dashboard:jobs"))
    assert response.status_code == 302
    assert "next=/dashboard/" in response["Location"]

    response = client.post(
        reverse("app_dashboard:login"),
        {"username": staff_user.email, "password": "test-password", "next": reverse("app_dashboard:jobs")},
    )
    assert response.status_code == 302
    assert response["Location"].endswith(reverse("app_dashboard:jobs"))
    assert client.get(reverse("app_dashboard:jobs")).status_code == 200


@pytest.mark.django_db
def test_non_staff_user_gets_forbidden(db):
    from app_users.models import User

    user = User.objects.create_user(
        email="reader@example.org",
        username="reader",
        password="test-password",
        is_staff=False,
    )
    client = Client()
    client.force_login(user)
    assert client.get(reverse("app_dashboard:jobs")).status_code == 403


@pytest.mark.django_db
def test_empty_jobs_page_is_functional(staff_user):
    client = Client()
    client.force_login(staff_user)
    response = client.get(reverse("app_dashboard:jobs"))
    assert response.status_code == 200
    assert "No Jobs to show" in response.content.decode()


@pytest.mark.django_db
def test_base_layout_contains_footer_social_links_and_theme_control(staff_user):
    client = Client()
    client.force_login(staff_user)
    body = client.get(reverse("app_dashboard:jobs")).content.decode()

    assert "Copyright © 2026, GiCP. All rights reserved." in body
    assert 'href="https://gicp.es/"' in body
    assert 'href="https://x.com/gicp_es"' in body
    assert 'href="/admin/"' in body
    assert ">Admin</a>" in body
    assert "data-theme-toggle" in body
    assert body.count("app_dashboard/dashboard.js") == 1


@pytest.mark.django_db
def test_dashboard_forms_use_compact_components_and_keep_new_job_out_of_navigation(staff_user):
    client = Client()
    client.force_login(staff_user)

    response = client.get(reverse("app_dashboard:job-new"))
    body = response.content.decode()

    assert response.status_code == 200
    assert "data-list-editor" in body
    assert "data-list-value" in body
    assert "toggle-track" in body
    assert "Advanced options" in body
    assert ">New Job</a>" not in body

    response = client.get(reverse("app_dashboard:schedule-new"))
    body = response.content.decode()
    assert response.status_code == 200
    assert "toggle-track" in body
    assert "Calendar options" in body
    assert body.count("Advanced options") == 1


def test_pipeline_forms_expose_searchable_catalogues_for_scope_fields():
    form = JobLaunchForm(pipeline_key="telegram.scan.rooms")

    assert form.fields["room"].dashboard_autocomplete_kind == "rooms"
    assert form.fields["tags"].dashboard_autocomplete_kind == "tags"
    assert form.fields["lang"].dashboard_autocomplete_kind == "languages"
    assert form.fields["room"].dashboard_autocomplete_url.endswith("/dashboard/lookup/rooms/")


def test_schedule_boolean_options_use_the_dashboard_toggle_widget():
    form = ScheduleForm(pipeline_key="telegram.scan.rooms")

    for name in ("enabled", "auto_finalize_failed", "replace_running_job"):
        assert "toggle-input" in form.fields[name].widget.attrs["class"]


def test_schedule_retry_margin_is_limited_by_the_repeat_cadence():
    base = {
        "name": "Short interval",
        "pipeline": "telegram.scan.rooms",
        "schedule_kind": "repeat",
        "repeat_every": "1",
        "repeat_period": "minutes",
        "advanced_options_touched": "1",
    }

    invalid = ScheduleForm(
        pipeline_key="telegram.scan.rooms",
        data={**base, "retry_grace_seconds": "16"},
    )
    valid = ScheduleForm(
        pipeline_key="telegram.scan.rooms",
        data={**base, "retry_grace_seconds": "15"},
    )

    assert not invalid.is_valid()
    assert "cannot exceed 15 seconds" in str(invalid.errors["retry_grace_seconds"])
    assert valid.is_valid(), valid.errors


def test_split_list_field_normalizes_values_for_the_list_editor():
    field = SplitListField()
    assert field.clean("room-one, room-two\nroom-one") == ["room-one", "room-two"]


def test_job_filter_form_validates_start_and_finish_ranges():
    valid = JobFilterForm(
        data={
            "started_from": "2026-01-01",
            "started_until": "2026-01-31",
            "finished_from": "2026-02-01",
            "finished_until": "2026-02-28",
        }
    )
    assert valid.is_valid(), valid.errors

    invalid = JobFilterForm(
        data={
            "started_from": "2026-02-01",
            "started_until": "2026-01-01",
        }
    )
    assert not invalid.is_valid()
    assert "started_until" in invalid.errors


@pytest.mark.django_db
def test_dashboard_uses_spanish_catalog_when_requested(staff_user):
    client = Client()
    client.force_login(staff_user)
    response = client.get(reverse("app_dashboard:jobs"), HTTP_ACCEPT_LANGUAGE="es")
    body = response.content.decode()
    assert response.status_code == 200
    assert "No hay Jobs que mostrar" in body
    assert "No Jobs to show" not in body


@pytest.mark.django_db
def test_detail_hides_domain_field_names_and_shows_job_batch_graph(staff_user):
    job = make_job()
    child = make_job(
        processor="telegram.process_messages",
        parent_job=job,
        workflow=["entity.ner.extract", "entity.ner.store"],
    )
    ProcessingBatch.objects.create(
        job=job,
        ordinal=1,
        status=ProcessingBatch.RUNNING,
        item_count=10,
        current_stage="entity.ner.extract",
        completed_stages=["telegram.scan.rooms.collect_messages"],
        stage_attempts={"entity.ner.extract": 1},
        result_summary={
            "schema_version": 1,
            "stages": {
                "telegram.scan.rooms.collect_messages": {
                    "inputs": [
                        {"key": "rooms", "count": 3, "kind": "telegram.room", "unit": "rooms"},
                        {"key": "empty_rooms", "count": 0, "kind": "telegram.room", "unit": "rooms"},
                        {"key": "unknown_rooms", "count": None, "kind": "telegram.room", "unit": "rooms"},
                    ],
                    "processed": [{"key": "rooms_scanned", "count": 3, "kind": "telegram.room", "unit": "rooms"}],
                    "outputs": [
                        {"key": "messages_recovered", "count": 12, "kind": "telegram.message", "unit": "messages"},
                        {"key": "extracted_entities", "count": 8, "kind": "entity", "unit": "entities"},
                        {"key": "canonical_urls", "count": 0, "kind": "url", "unit": "urls"},
                        {"key": "unknown_results", "count": None, "kind": "record", "unit": "records"},
                    ],
                }
            },
        },
    )
    ProcessingBatch.objects.create(
        job=child,
        ordinal=1,
        status=ProcessingBatch.COMPLETED,
        item_count=10,
        current_stage="",
        completed_stages=["entity.ner.extract", "entity.ner.store", "telegram.index.messages.write"],
        result_summary={
            "schema_version": 1,
            "stages": {
                "entity.ner.extract": {
                    "inputs": [{"key": "messages", "count": 10, "kind": "telegram.message", "unit": "messages"}],
                    "processed": [{"key": "messages_sent_to_ner", "count": 9, "kind": "telegram.message", "unit": "messages"}],
                    "outputs": [{"key": "extracted_entities", "count": 8, "kind": "entity", "unit": "entities"}],
                },
                "entity.ner.store": {
                    "inputs": [
                        {"key": "messages", "count": 9, "kind": "telegram.message", "unit": "messages"},
                        {"key": "extracted_entities", "count": 8, "kind": "entity", "unit": "entities"},
                    ],
                },
            },
        },
    )
    client = Client()
    client.force_login(staff_user)

    response = client.get(reverse("app_dashboard:job-detail", args=[job.pk]))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Job" in body and "Batch" in body
    assert "Process messages" in body
    assert "Detect entities" in body
    assert "ProcessingJob" not in body
    assert "estimated_batches_total" not in body
    assert 'aria-label="Running"' in body
    assert "progress-track-running" in body
    assert "Sequential summary" not in body
    assert 'data-dag-graph' in body
    assert 'data-dag-canvas' in body
    assert 'data-port="in"' in body
    assert 'data-port="out"' in body
    assert "Work units" not in body
    assert "Rooms scanned" in body
    assert "Messages recovered" in body
    assert "Extracted entities" in body
    assert "Canonical urls" in body
    assert "Unknown results" in body
    assert 'data-batch-detail-dialog' in body
    assert 'data-batch-detail-json=' in body
    assert 'data-stage-open' in body
    assert 'data-dag-metric-more' in body
    assert ">+2</button>" in body
    assert "Count unavailable in summary" in body
    assert 'class="dag-input-summary"' not in body
    assert 'class="dag-stage-result-values"' not in body
    assert "Workflow summary" in body
    assert "dag-stage-results" in body
    assert "Deferred" not in body

    response = client.get(reverse("app_dashboard:jobs"))
    assert "Derived operations" not in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("job_ids", [(3, 1, 2), (1, 3, 2)])
def test_workflow_summary_follows_pipeline_ranks_and_workflow_order(job_ids):
    root = make_job(
        id=UUID(int=job_ids[0]),
        workflow=["telegram.scan.rooms.collect_messages"],
    )
    child = make_job(
        id=UUID(int=job_ids[1]),
        parent_job=root,
        workflow=["entity.ner.extract", "entity.ner.store"],
    )
    grandchild = make_job(
        id=UUID(int=job_ids[2]),
        parent_job=child,
        workflow=["metadata.embeddings.generate_and_sync", "metadata.sentiment.classify_and_store"],
    )
    for job in (root, child, grandchild):
        for ordinal in (1, 2):
            ProcessingBatch.objects.create(
                job=job,
                ordinal=ordinal,
                status=ProcessingBatch.COMPLETED,
                completed_stages=job.workflow,
                result_summary={
                    "schema_version": 1,
                    "stages": {
                        stage: {
                            "outputs": [{"key": "results", "count": ordinal, "kind": "record"}],
                        }
                        for stage in reversed(job.workflow)
                    },
                },
            )

    summary = build_job_graph(root)["workflow_summary"]

    assert [stage["key"] for stage in summary] == root.workflow + child.workflow + grandchild.workflow
    assert all(stage["outputs"][0]["count"] == 3 for stage in summary)
    assert all(stage["completed"] == 2 and stage["state"] == "completed" for stage in summary)


@pytest.mark.django_db
def test_graph_aggregates_batches_by_stage():
    job = make_job()
    ProcessingBatch.objects.create(
        job=job,
        ordinal=1,
        status=ProcessingBatch.COMPLETED,
        current_stage="entity.ner.extract",
        completed_stages=["telegram.scan.rooms.collect_messages"],
    )
    ProcessingBatch.objects.create(
        job=job,
        ordinal=2,
        status=ProcessingBatch.PENDING,
        current_stage="",
        completed_stages=[],
    )
    graph = build_job_graph(job)
    stages = graph["root"]["stages"]
    assert stages[0]["completed"] == 1
    assert stages[0]["pending"] == 1
    assert stages[1]["state"] == "pending"
    assert [batch["ordinal"] for batch in graph["root"]["batches"]] == [1, 2]
    assert graph["root"]["batches"][0]["stages"][0]["state"] == "completed"


@pytest.mark.django_db
def test_graph_keeps_each_batch_and_its_stage_state_visible():
    job = make_job(workflow=["entity.ner.extract", "entity.ner.store"])
    ProcessingBatch.objects.create(
        job=job,
        ordinal=1,
        status=ProcessingBatch.COMPLETED,
        completed_stages=["entity.ner.extract", "entity.ner.store"],
    )
    ProcessingBatch.objects.create(
        job=job,
        ordinal=2,
        status=ProcessingBatch.RUNNING,
        current_stage="entity.ner.extract",
    )
    ProcessingBatch.objects.create(
        job=job,
        ordinal=3,
        status=ProcessingBatch.FAILED,
        current_stage="entity.ner.store",
        completed_stages=["entity.ner.extract"],
    )

    batches = build_job_graph(job)["root"]["batches"]

    assert [batch["ordinal"] for batch in batches] == [1, 2, 3]
    assert batches[0]["stages"][1]["state"] == "completed"
    assert batches[1]["stages"][0]["state"] == "running"
    assert batches[2]["stages"][1]["state"] == "failed"


@pytest.mark.django_db
def test_pipeline_graph_keeps_materialized_and_pending_child_handoffs():
    parent = make_job(workflow=["telegram.scan.rooms.collect_messages"])
    child = make_job(
        processor="telegram.process_messages",
        parent_job=parent,
        workflow=["entity.ner.extract"],
    )
    source = ProcessingBatch.objects.create(
        job=parent,
        ordinal=1,
        status=ProcessingBatch.COMPLETED,
        completed_stages=["telegram.scan.rooms.collect_messages"],
    )
    pending_source = ProcessingBatch.objects.create(
        job=parent,
        ordinal=2,
        status=ProcessingBatch.COMPLETED,
        completed_stages=["telegram.scan.rooms.collect_messages"],
    )
    target = ProcessingBatch.objects.create(job=child, ordinal=1, status=ProcessingBatch.PENDING)
    item_type = ContentType.objects.get_for_model(ProcessingJob, for_concrete_model=False)
    ProcessingHandoff.objects.create(
        source_batch=source,
        target_job=child,
        target_batch=target,
        item_content_type=item_type,
        item_ids=["target-item"],
        item_count=1,
        status=ProcessingHandoff.MATERIALIZED,
    )
    pending = ProcessingHandoff.objects.create(
        source_batch=pending_source,
        target_job=child,
        item_content_type=item_type,
        item_ids=["pending-item"],
        item_count=1,
    )

    pipeline = build_job_graph(parent)["pipeline"]
    child_node = pipeline["columns"][1]["jobs"][0]
    task_ids = {task["id"] for task in child_node["tasks"]}
    edge_pairs = {(edge["source"], edge["target"]) for edge in pipeline["edges"]}

    assert task_ids == {f"batch-{target.pk}", f"waiting-{pending.pk}"}
    assert (f"batch-{source.pk}", f"batch-{target.pk}") in edge_pairs
    assert (f"batch-{pending_source.pk}", f"waiting-{pending.pk}") in edge_pairs


@pytest.mark.django_db
def test_large_pipeline_defers_cards_and_metrics_but_keeps_all_batch_positions(staff_user):
    job = make_job(workflow=["entity.ner.extract"])
    ProcessingBatch.objects.bulk_create([
        ProcessingBatch(job=job, ordinal=ordinal, item_count=10)
        for ordinal in range(1, 1001)
    ])
    client = Client()
    client.force_login(staff_user)
    with patch("app_dashboard.services.graph.present_batch", side_effect=AssertionError("eager batch details")):
        response = client.get(reverse("app_dashboard:job-graph", args=[job.pk]))
    body = response.content.decode()
    assert response.status_code == 200
    assert 'data-dag-virtual="true"' in body
    assert "data-dag-virtual-list" in body
    assert "Batch 1000" in body
    assert "data-batch-detail-json=" not in body
    assert "<article" not in body


@pytest.mark.django_db
def test_graph_poll_sends_one_changed_card_and_requests_rebuild_for_new_batches(staff_user):
    job = make_job(workflow=["entity.ner.extract"])
    first = ProcessingBatch.objects.create(job=job, ordinal=1)
    second = ProcessingBatch.objects.create(job=job, ordinal=2)
    snapshot = build_job_pipeline(job)["pipeline"]["snapshot"]
    client = Client()
    client.force_login(staff_user)
    url = reverse("app_dashboard:job-graph", args=[job.pk])
    params = {"format": "json", "since": snapshot["cursor"], "topology": snapshot["topology"], "revision": snapshot["revision"]}
    unchanged = client.get(url, params).json()
    assert unchanged["unchanged"] is True
    assert "updates" not in unchanged

    second.status = ProcessingBatch.RUNNING
    second.current_stage = "entity.ner.extract"
    second.save(update_fields=["status", "current_stage", "updated_at"])
    updates = client.get(url, params).json()
    assert "reload" not in updates
    assert [row["id"] for row in updates["updates"]] == [f"batch-{second.pk}"]
    assert "data-stage-open" in updates["updates"][0]["html"]
    assert "Running" in updates["updates"][0]["html"]
    assert str(first.pk) not in updates["updates"][0]["html"]

    ProcessingBatch.objects.create(job=job, ordinal=3)
    structure = client.get(url, params).json()["structure"]
    assert len(structure["columns"][0]["jobs"][0]["tasks"]) == 3


@pytest.mark.django_db
def test_large_pipeline_poll_avoids_refreshing_unchanged_batches_when_job_counters_change():
    job = make_job(workflow=["entity.ner.extract"])
    batches = ProcessingBatch.objects.bulk_create([
        ProcessingBatch(job=job, ordinal=ordinal) for ordinal in range(1, 201)
    ])
    snapshot = build_job_pipeline(job)["pipeline"]["snapshot"]
    batch = batches[-1]
    batch.status = ProcessingBatch.COMPLETED
    batch.completed_stages = ["entity.ner.extract"]
    batch.save(update_fields=["status", "completed_stages", "updated_at"])
    job.completed_batches = 1
    job.save(update_fields=["completed_batches", "updated_at"])
    with patch("app_dashboard.services.graph.present_batch", side_effect=AssertionError("eager batch details")):
        response = build_job_pipeline_updates(
            job, since=parse_datetime(snapshot["cursor"]),
            topology=snapshot["topology"], revision=snapshot["revision"],
            job_states={str(job.pk): job.status},
        )
    assert [row["id"] for row in response["updates"]] == [f"batch-{batch.pk}"]
    assert "html" not in response["updates"][0]


@pytest.mark.django_db
def test_lazy_batch_detail_keeps_metrics_and_dependencies_and_enforces_job_scope(staff_user):
    root = make_job(workflow=["entity.ner.extract"])
    child = make_job(parent_job=root, workflow=["entity.ner.extract"])
    source = ProcessingBatch.objects.create(job=root, ordinal=1, status=ProcessingBatch.COMPLETED)
    target = ProcessingBatch.objects.create(
        job=child, ordinal=1, item_count=8,
        result_summary={"schema_version": 1, "stages": {"entity.ner.extract": {
            "inputs": [{"key": "messages", "count": 8, "kind": "telegram.message", "unit": "messages"}],
        }}},
    )
    ProcessingHandoff.objects.create(
        source_batch=source, target_job=child, target_batch=target,
        item_content_type=ContentType.objects.get_for_model(ProcessingJob),
        status=ProcessingHandoff.MATERIALIZED, item_count=8,
    )
    outside = ProcessingBatch.objects.create(job=make_job(), ordinal=1)
    client = Client()
    url = reverse("app_dashboard:job-batch-detail", args=[root.pk])
    assert client.get(url, {"batch": str(target.pk)}).status_code == 302
    client.force_login(staff_user)
    response = client.get(url, {"batch": str(target.pk)})
    assert response.status_code == 200
    data = response.json()
    assert data["batch_id"] == str(target.pk)
    assert data["dependencies"][0]["id"] == str(source.pk)
    assert data["dependencies"][0]["transfer_count"] == 8
    assert data["stages"][0]["inputs"][0]["count"] == 8
    assert client.get(url, {"batch": str(outside.pk)}).status_code == 404
    assert client.get(url, {"batch": "invalid"}).status_code == 404


@pytest.mark.django_db
def test_graph_refreshes_a_dependency_when_only_its_source_batch_changes():
    root = make_job(workflow=["entity.ner.extract"])
    child = make_job(parent_job=root, workflow=["entity.ner.extract"])
    source = ProcessingBatch.objects.create(job=root, ordinal=1)
    target = ProcessingBatch.objects.create(job=child, ordinal=1)
    ProcessingHandoff.objects.create(
        source_batch=source, target_job=child, target_batch=target,
        item_content_type=ContentType.objects.get_for_model(ProcessingJob),
        status=ProcessingHandoff.MATERIALIZED, item_count=8,
    )
    snapshot = build_job_pipeline(root)["pipeline"]["snapshot"]
    source.status = ProcessingBatch.FAILED
    source.save(update_fields=["status", "updated_at"])
    response = build_job_pipeline_updates(
        root, since=parse_datetime(snapshot["cursor"]),
        topology=snapshot["topology"], revision=snapshot["revision"],
    )
    assert {row["id"] for row in response["updates"]} == {f"batch-{source.pk}", f"batch-{target.pk}"}
    target_row = next(row for row in response["updates"] if row["id"] == f"batch-{target.pk}")
    assert "Failed" in target_row["html"]


@pytest.mark.django_db
def test_pending_handoff_message_refreshes_when_its_job_is_paused():
    root = make_job(workflow=["entity.ner.extract"])
    child = make_job(parent_job=root, workflow=["entity.ner.extract"])
    source = ProcessingBatch.objects.create(job=root, ordinal=1)
    pending = ProcessingHandoff.objects.create(
        source_batch=source, target_job=child,
        item_content_type=ContentType.objects.get_for_model(ProcessingJob), item_count=8,
    )
    snapshot = build_job_pipeline(root)["pipeline"]["snapshot"]
    child.status = ProcessingJob.PAUSED
    child.save(update_fields=["status", "updated_at"])
    response = build_job_pipeline_updates(
        root, since=parse_datetime(snapshot["cursor"]),
        topology=snapshot["topology"], revision=snapshot["revision"],
        job_states={str(root.pk): root.status, str(child.pk): ProcessingJob.RUNNING},
    )
    waiting = next(row for row in response["updates"] if row["id"] == f"waiting-{pending.pk}")
    assert "1 pending handoff" in waiting["html"]
    assert "This Job is paused" in waiting["html"]


@pytest.mark.django_db
def test_dashboard_lookup_returns_staff_only_room_tag_and_language_options(staff_user, room_factory):
    room_factory(unique_name="research_room", title="Research room", tags=["Research", "News"])
    client = Client()
    client.force_login(staff_user)

    rooms = client.get(reverse("app_dashboard:lookup", args=["rooms"]), {"q": "research"})
    assert rooms.status_code == 200
    assert rooms.json()["results"][0]["value"] == "research_room"

    tags = client.get(reverse("app_dashboard:lookup", args=["tags"]), {"q": "res"})
    assert tags.status_code == 200
    assert tags.json()["results"][0]["value"] == "research"

    languages = client.get(reverse("app_dashboard:lookup", args=["languages"]), {"q": "spa"})
    assert languages.status_code == 200
    assert languages.json()["results"][0]["value"] == "es"


@pytest.mark.django_db
def test_job_form_rejects_unknown_pipeline():
    form = JobLaunchForm(
        pipeline_key="telegram.scan.rooms",
        data={"pipeline": "not-registered"},
    )
    assert not form.is_valid()
    assert "pipeline" in form.errors


@pytest.mark.django_db
def test_dashboard_schedule_create_edit_toggle_delete(staff_user):
    client = Client()
    client.force_login(staff_user)
    url = reverse("app_dashboard:schedule-new")
    payload = {
        "name": "Escaneo de prueba",
        "description": "Operación periódica",
        "pipeline": "telegram.scan.rooms",
        "schedule_kind": "repeat",
        "repeat_every": "5",
        "repeat_period": "minutes",
        "enabled": "on",
    }
    response = client.post(url, payload)
    assert response.status_code == 302
    trigger = PeriodicTask.objects.get(name="Escaneo de prueba")
    assert trigger.enabled is True
    assert trigger.task == "app_telegram.services.pipelines.scan_pipeline"

    response = client.get(reverse("app_dashboard:schedules"))
    body = response.content.decode()
    assert response.status_code == 200
    assert "data-schedule-toggle" in body
    assert "schedule-state" not in body
    assert ">Disable<" not in body
    assert ">Enable<" not in body

    response = client.post(reverse("app_dashboard:schedule-toggle", args=[trigger.pk]))
    assert response.status_code == 302
    trigger.refresh_from_db()
    assert trigger.enabled is False

    response = client.post(reverse("app_dashboard:schedule-delete", args=[trigger.pk]))
    assert response.status_code == 302
    assert not PeriodicTask.objects.filter(pk=trigger.pk).exists()


@pytest.mark.django_db
def test_clocked_schedule_requires_one_off_and_pipeline_cannot_change():
    invalid = ProcessingTriggerSerializer(
        data={
            "name": "Una vez",
            "pipeline": "telegram.scan.rooms",
            "clocked": {"clocked_time": "2030-01-01T10:00:00Z"},
            "one_off": False,
        }
    )
    assert not invalid.is_valid()

    schedule = ScheduleForm(
        pipeline_key="telegram.scan.rooms",
        data={
            "name": "Una vez",
            "pipeline": "telegram.scan.rooms",
            "schedule_kind": "once",
            "once_time": "2030-01-01T10:00",
        },
    )
    assert schedule.is_valid(), schedule.errors
    payload = schedule.trigger_payload()
    assert payload["one_off"] is True
    assert set(payload) & {"clocked"}

    untouched = ScheduleForm(
        pipeline_key="telegram.scan.rooms",
        data={
            "name": "Sin ajustes avanzados",
            "pipeline": "telegram.scan.rooms",
            "schedule_kind": "repeat",
            "repeat_every": "4",
            "repeat_period": "hours",
            "advanced_options_touched": "0",
            "auto_finalize_failed": "false",
            "replace_running_job": "false",
        },
    )
    assert untouched.is_valid(), untouched.errors
    assert untouched.trigger_payload()["auto_finalize_failed"] is True

    touched = ScheduleForm(
        pipeline_key="telegram.scan.rooms",
        data={
            "name": "Con ajustes avanzados",
            "pipeline": "telegram.scan.rooms",
            "schedule_kind": "repeat",
            "repeat_every": "4",
            "repeat_period": "hours",
            "advanced_options_touched": "1",
            "auto_finalize_failed": "false",
            "replace_running_job": "false",
        },
    )
    assert touched.is_valid(), touched.errors
    assert touched.trigger_payload()["auto_finalize_failed"] is False


@pytest.mark.django_db
def test_job_actions_are_csrf_protected(staff_user):
    job = make_job()
    client = Client(enforce_csrf_checks=True)
    client.force_login(staff_user)
    response = client.post(reverse("app_dashboard:job-action", args=[job.pk, "pause"]))
    assert response.status_code == 403
