import json

import pytest
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from rest_framework import status

from app_base.models import Query
from app_scheduler.serializers import ProcessingTriggerSerializer
from app_scheduler.registry import get_scheduled_pipeline_task_names
from app_scheduler.services.api import public_trigger_queryset, search_triggers


TRIGGERS_URL = "/api/v1/scheduler/triggers"
SCAN_PIPELINE = "telegram.scan.rooms"
SCAN_TASK = "app_telegram.services.pipelines.scan_pipeline"
INDEX_PIPELINE = "telegram.index.messages"
INDEX_TASK = "app_telegram.services.pipelines.index_msgs_pipeline"
INTERNAL_TASKS = (
    "celery.backend_cleanup",
    "app_scheduler.tasks.reconcile_processing_jobs",
)


def trigger_payload(**overrides):
    payload = {
        "name": "scan-every-five-minutes",
        "pipeline": SCAN_PIPELINE,
        "interval": {"every": 5, "period": IntervalSchedule.MINUTES},
        "parameters": {"room": ["room_a"], "tags": ["crypto"]},
        "enabled": False,
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_trigger_requires_exactly_one_schedule():
    without_schedule = ProcessingTriggerSerializer(
        data={"name": "missing", "pipeline": SCAN_PIPELINE}
    )
    with_two_schedules = ProcessingTriggerSerializer(
        data=trigger_payload(
            crontab={
                "minute": "0",
                "hour": "0",
                "day_of_week": "*",
                "day_of_month": "*",
                "month_of_year": "*",
                "timezone": "UTC",
            }
        )
    )

    assert not without_schedule.is_valid()
    assert "non_field_errors" in without_schedule.errors
    assert not with_two_schedules.is_valid()
    assert "non_field_errors" in with_two_schedules.errors


@pytest.mark.django_db
def test_clocked_trigger_must_be_one_off():
    serializer = ProcessingTriggerSerializer(
        data={
            "name": "clocked-trigger",
            "pipeline": SCAN_PIPELINE,
            "clocked": {"clocked_time": "2030-01-01T10:00:00Z"},
            "one_off": False,
        }
    )

    assert not serializer.is_valid()
    assert "clocked" in serializer.errors


@pytest.mark.django_db
def test_trigger_rejects_arbitrary_celery_task_and_stage_keys():
    raw_task = ProcessingTriggerSerializer(data=trigger_payload(task=SCAN_TASK))
    stage_key = ProcessingTriggerSerializer(
        data=trigger_payload(pipeline="telegram.scan.rooms.collect_messages")
    )

    assert not raw_task.is_valid()
    assert "task" in raw_task.errors
    assert not stage_key.is_valid()
    assert "pipeline" in stage_key.errors


@pytest.mark.django_db
def test_trigger_validates_parameters_against_pipeline_serializer():
    invalid_filter = ProcessingTriggerSerializer(
        data=trigger_payload(parameters={"max_msgs": 9})
    )
    unknown_parameter = ProcessingTriggerSerializer(
        data=trigger_payload(parameters={"not_a_scan_option": True})
    )
    old_filter_alias = ProcessingTriggerSerializer(
        data=trigger_payload(parameters={"tag": ["crypto"]})
    )

    assert not invalid_filter.is_valid()
    assert "parameters" in invalid_filter.errors
    assert not unknown_parameter.is_valid()
    assert "parameters" in unknown_parameter.errors
    assert not old_filter_alias.is_valid()
    assert "parameters" in old_filter_alias.errors


@pytest.mark.django_db
def test_trigger_saves_task_mapping_and_parameters_matching_launcher_signature():
    serializer = ProcessingTriggerSerializer(data=trigger_payload())

    assert serializer.is_valid(), serializer.errors
    trigger = serializer.save()

    assert trigger.task == SCAN_TASK
    stored_kwargs = json.loads(trigger.kwargs)
    assert stored_kwargs.pop("scheduler_context") == {
        "auto_finalize_failed": True,
        "replace_running_job": False,
        "retry_grace_seconds": 300,
        "scheduled": True,
        "trigger_id": trigger.pk,
        "schedule": {
            "type": "interval",
            "every": 5,
            "period": IntervalSchedule.MINUTES,
        },
    }
    assert stored_kwargs == {
        "check_room": False,
        "room": ["room_a"],
        "tag_match": "any",
        "tags": ["crypto"],
        "is_channel": None,
    }
    assert trigger.args == "[]"
    assert ProcessingTriggerSerializer(trigger).data["pipeline"] == SCAN_PIPELINE
    assert ProcessingTriggerSerializer(trigger).data["parameters"]["tags"] == ["crypto"]


@pytest.mark.django_db
def test_trigger_lifecycle_options_are_public_but_internal_context_stays_private():
    serializer = ProcessingTriggerSerializer(
        data=trigger_payload(
            auto_finalize_failed=False,
            replace_running_job=True,
            retry_grace_seconds=45,
        )
    )

    assert serializer.is_valid(), serializer.errors
    trigger = serializer.save()
    representation = ProcessingTriggerSerializer(trigger).data
    stored_kwargs = json.loads(trigger.kwargs)

    assert representation["auto_finalize_failed"] is False
    assert representation["replace_running_job"] is True
    assert representation["retry_grace_seconds"] == 45
    assert "scheduler_context" not in representation["parameters"]
    assert stored_kwargs["scheduler_context"]["auto_finalize_failed"] is False
    assert stored_kwargs["scheduler_context"]["replace_running_job"] is True
    assert stored_kwargs["scheduler_context"]["retry_grace_seconds"] == 45


@pytest.mark.django_db
def test_trigger_pipeline_is_immutable_after_creation():
    serializer = ProcessingTriggerSerializer(data=trigger_payload())
    assert serializer.is_valid(), serializer.errors
    trigger = serializer.save()

    update = ProcessingTriggerSerializer(
        trigger,
        data={"pipeline": INDEX_PIPELINE},
        partial=True,
    )

    assert not update.is_valid()
    assert "pipeline" in update.errors


@pytest.mark.django_db
def test_scheduler_search_filters_public_pipeline_triggers_and_hides_internal_beat_rows():
    schedule = IntervalSchedule.objects.create(every=10, period=IntervalSchedule.MINUTES)
    matching = PeriodicTask.objects.create(
        name="daily-telegram-scan",
        task=SCAN_TASK,
        interval=schedule,
        description="Collect messages",
    )
    PeriodicTask.objects.create(
        name="manual-index-trigger",
        task=INDEX_TASK,
        interval=schedule,
        description="Index messages",
    )
    for name, task in zip(("cleanup", "reconcile"), INTERNAL_TASKS):
        PeriodicTask.objects.create(name=name, task=task, interval=schedule)

    results = search_triggers(
        pipeline=SCAN_PIPELINE,
        name="telegram",
        description="collect",
        schedule_type="interval",
    )

    assert list(results) == [matching]
    assert list(search_triggers()) == list(
        PeriodicTask.objects.filter(task__in=(SCAN_TASK, INDEX_TASK)).order_by("name")
    )


@pytest.mark.django_db
def test_trigger_api_requires_staff_user(api_client):
    response = api_client.get(TRIGGERS_URL)

    assert response.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }


@pytest.mark.django_db
def test_trigger_api_lists_only_registered_pipeline_rows(api_client, staff_user):
    schedule = IntervalSchedule.objects.create(every=15, period=IntervalSchedule.MINUTES)
    PeriodicTask.objects.create(
        name="listed-scan",
        task=SCAN_TASK,
        interval=schedule,
        kwargs='{"tag_match": "any"}',
    )
    PeriodicTask.objects.create(
        name="hidden-cleanup",
        task=INTERNAL_TASKS[0],
        interval=schedule,
    )
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(TRIGGERS_URL, {"pipeline": SCAN_PIPELINE})

    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == 1, response.data
    assert response.data["results"][0]["name"] == "listed-scan"
    assert response.data["results"][0]["pipeline"] == SCAN_PIPELINE
    assert "task" not in response.data["results"][0]


@pytest.mark.django_db
def test_trigger_api_rejects_old_task_filter(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(TRIGGERS_URL, {"task": SCAN_TASK})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "task" in response.data


@pytest.mark.django_db
def test_trigger_api_creates_periodic_pipeline_definition(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    response = api_client.post(TRIGGERS_URL, trigger_payload(), format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["token"]
    result = response.data["result"]
    assert result["pipeline"] == SCAN_PIPELINE
    assert result["enabled"] is False
    assert Query.objects.count() == 1
    trigger = PeriodicTask.objects.get(name="scan-every-five-minutes")
    assert trigger.task == SCAN_TASK
    assert json.loads(trigger.kwargs)["tags"] == ["crypto"]


@pytest.mark.django_db
def test_new_trigger_is_disabled_by_default():
    payload = trigger_payload()
    payload.pop("enabled")
    serializer = ProcessingTriggerSerializer(data=payload)

    assert serializer.is_valid(), serializer.errors
    assert serializer.save().enabled is False


@pytest.mark.django_db
def test_trigger_api_can_disable_existing_scan_and_keeps_its_configuration(api_client, staff_user):
    interval = IntervalSchedule.objects.create(every=6, period=IntervalSchedule.HOURS)
    trigger = PeriodicTask.objects.create(
        name="scan-each-6h",
        task=SCAN_TASK,
        interval=interval,
        kwargs='{"tag_match": "any"}',
        enabled=True,
    )
    api_client.force_authenticate(user=staff_user)

    response = api_client.patch(
        f"{TRIGGERS_URL}/{trigger.pk}",
        {"enabled": False},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.data["token"]
    assert response.data["result"]["enabled"] is False
    assert Query.objects.count() == 1
    trigger.refresh_from_db()
    assert trigger.enabled is False
    assert trigger.name == "scan-each-6h"
    assert trigger.task == SCAN_TASK
    assert trigger.interval.every == 6
    assert trigger.interval.period == IntervalSchedule.HOURS
    stored_kwargs = json.loads(trigger.kwargs)
    context = stored_kwargs.pop("scheduler_context")
    assert context["auto_finalize_failed"] is True
    assert context["replace_running_job"] is False
    assert context["retry_grace_seconds"] == 300
    assert context["trigger_id"] == trigger.pk
    assert context["schedule"] == {
        "type": "interval",
        "every": 6,
        "period": IntervalSchedule.HOURS,
    }
    assert stored_kwargs == {"tag_match": "any"}


@pytest.mark.django_db
def test_trigger_api_does_not_expose_old_task_route_or_allow_mutating_internal_rows(api_client, staff_user):
    interval = IntervalSchedule.objects.create(every=60, period=IntervalSchedule.SECONDS)
    internal = PeriodicTask.objects.create(
        name="reconcile-durable-processing-jobs",
        task=INTERNAL_TASKS[1],
        interval=interval,
        enabled=True,
    )
    api_client.force_authenticate(user=staff_user)

    old_route = api_client.get("/api/v1/scheduler/task")
    internal_detail = api_client.get(f"{TRIGGERS_URL}/{internal.pk}")
    internal_update = api_client.patch(
        f"{TRIGGERS_URL}/{internal.pk}",
        {"enabled": False},
        format="json",
    )
    internal_delete = api_client.delete(f"{TRIGGERS_URL}/{internal.pk}")

    assert old_route.status_code == status.HTTP_404_NOT_FOUND
    assert internal_detail.status_code == status.HTTP_404_NOT_FOUND
    assert internal_update.status_code == status.HTTP_404_NOT_FOUND
    assert internal_update.data["token"]
    assert internal_delete.status_code == status.HTTP_404_NOT_FOUND
    assert internal_delete.data["token"]
    assert Query.objects.count() == 2
    assert PeriodicTask.objects.filter(pk=internal.pk).exists()
    internal.refresh_from_db()
    assert internal.enabled is True


@pytest.mark.django_db
def test_trigger_api_delete_uses_common_audit_and_204_response(api_client, staff_user):
    interval = IntervalSchedule.objects.create(every=6, period=IntervalSchedule.HOURS)
    trigger = PeriodicTask.objects.create(
        name="delete-this-scan",
        task=SCAN_TASK,
        interval=interval,
    )
    api_client.force_authenticate(user=staff_user)

    response = api_client.delete(f"{TRIGGERS_URL}/{trigger.pk}")

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert Query.objects.count() == 1
    assert not public_trigger_queryset().filter(pk=trigger.pk).exists()


def test_all_registered_trigger_tasks_are_discoverable_by_celery():
    from atenea_api.celery import app

    app.loader.import_default_modules()

    assert get_scheduled_pipeline_task_names()
    assert set(get_scheduled_pipeline_task_names()).issubset(app.tasks)
