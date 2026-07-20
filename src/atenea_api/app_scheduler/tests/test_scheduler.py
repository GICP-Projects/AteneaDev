import json

import pytest
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from rest_framework import status

from app_scheduler.serializers import PeriodicTaskSerializer
from app_scheduler.services.api import search


SCHEDULER_URL = "/api/v1/scheduler/task"
SCAN_TASK = "app_telegram.services.pipelines.scan_pipeline"
INDEX_TASK = "app_telegram.services.pipelines.index_msgs_pipeline"


def interval_payload(**overrides):
    payload = {
        "name": "scan-every-five-minutes",
        "task": SCAN_TASK,
        "interval": {"every": 5, "period": IntervalSchedule.MINUTES},
        "kwargs": {"max_msgs": 100, "room": ["room_a"]},
        "enabled": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_periodic_task_requires_exactly_one_schedule():
    without_schedule = PeriodicTaskSerializer(
        data={"name": "missing", "task": SCAN_TASK}
    )
    with_two_schedules = PeriodicTaskSerializer(
        data=interval_payload(
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
def test_clocked_schedule_must_be_one_off():
    serializer = PeriodicTaskSerializer(
        data={
            "name": "clocked-task",
            "task": SCAN_TASK,
            "clocked": {"clocked_time": "2030-01-01T10:00:00Z"},
            "one_off": False,
        }
    )

    assert not serializer.is_valid()
    assert "non_field_errors" in serializer.errors


@pytest.mark.django_db
def test_periodic_task_validates_kwargs_with_task_serializer():
    serializer = PeriodicTaskSerializer(
        data=interval_payload(kwargs={"max_msgs": 9})
    )

    assert not serializer.is_valid()
    assert "kwargs" in serializer.errors


@pytest.mark.django_db
def test_periodic_task_create_persists_schedule_and_encoded_kwargs():
    serializer = PeriodicTaskSerializer(data=interval_payload())

    assert serializer.is_valid(), serializer.errors
    task = serializer.save()

    assert task.interval.every == 5
    assert task.interval.period == IntervalSchedule.MINUTES
    assert json.loads(task.kwargs) == {"room": ["room_a"], "max_msgs": 100, "tag_match": "any"}


@pytest.mark.django_db
def test_periodic_task_update_cannot_change_task():
    serializer = PeriodicTaskSerializer(data=interval_payload())
    assert serializer.is_valid(), serializer.errors
    task = serializer.save()

    update = PeriodicTaskSerializer(
        task,
        data={"task": INDEX_TASK},
        partial=True,
    )

    assert not update.is_valid()
    assert "task" in update.errors


@pytest.mark.django_db
def test_scheduler_search_filters_name_task_and_schedule_type():
    schedule = IntervalSchedule.objects.create(every=10, period=IntervalSchedule.MINUTES)
    matching = PeriodicTask.objects.create(
        name="daily-telegram-scan",
        task=SCAN_TASK,
        interval=schedule,
        description="Collect messages",
    )
    PeriodicTask.objects.create(
        name="other-job",
        task=INDEX_TASK,
        interval=schedule,
        description="Index messages",
    )

    results = search(
        task=SCAN_TASK,
        name="telegram",
        description="collect",
        schedule_type="interval",
    )

    assert list(results) == [matching]


@pytest.mark.django_db
def test_scheduler_endpoint_requires_staff(api_client):
    response = api_client.get(SCHEDULER_URL)

    assert response.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }


@pytest.mark.django_db
def test_scheduler_list_endpoint_filters_results(api_client, staff_user):
    schedule = IntervalSchedule.objects.create(every=15, period=IntervalSchedule.MINUTES)
    PeriodicTask.objects.create(
        name="listed-scan",
        task=SCAN_TASK,
        interval=schedule,
    )
    PeriodicTask.objects.create(
        name="hidden-index",
        task=INDEX_TASK,
        interval=schedule,
    )
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(SCHEDULER_URL, {"task": SCAN_TASK})

    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == 1
    assert response.data["results"][0]["name"] == "listed-scan"


@pytest.mark.django_db
def test_scheduler_list_endpoint_rejects_invalid_filter(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)

    response = api_client.get(SCHEDULER_URL, {"schedule-type": "solar"})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "errors" in response.data

