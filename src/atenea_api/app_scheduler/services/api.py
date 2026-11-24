"""Database access for public pipeline triggers backed by django-celery-beat."""

from django_celery_beat.models import PeriodicTask

from app_scheduler.registry import (
    get_scheduled_pipeline,
    get_scheduled_pipeline_task_names,
)


def _registered_task_names() -> list[str]:
    return get_scheduled_pipeline_task_names()


def public_trigger_queryset():
    """Return only Beat rows that represent registered public pipelines."""

    return PeriodicTask.objects.filter(task__in=_registered_task_names()).select_related(
        "interval", "crontab", "clocked"
    )


def search_triggers(
    *,
    pipeline=None,
    name=None,
    description=None,
    schedule_type=None,
    enabled=None,
):
    """Return only Beat rows whose task is a registered Job-creating launcher."""

    queryset = public_trigger_queryset()
    if pipeline:
        queryset = queryset.filter(task=get_scheduled_pipeline(pipeline).task)
    if name:
        queryset = queryset.filter(name__icontains=name)
    if description:
        queryset = queryset.filter(description__icontains=description)
    if schedule_type:
        queryset = queryset.filter(**{f"{schedule_type}__isnull": False})
    if enabled is not None:
        queryset = queryset.filter(enabled=enabled)
    return queryset.order_by("name")


def get_trigger(trigger_id):
    """Get an API-manageable trigger; internal Beat tasks deliberately 404."""

    try:
        trigger_id = int(trigger_id)
    except (TypeError, ValueError) as exc:
        raise PeriodicTask.DoesNotExist from exc
    return public_trigger_queryset().get(pk=trigger_id)
