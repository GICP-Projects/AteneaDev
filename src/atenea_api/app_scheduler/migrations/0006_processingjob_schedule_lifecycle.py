import json

import app_base.models
from django.db import migrations, models


SCHEDULED_TASKS = {
    "app_entity.services.pipelines.ner_extraction_msgs_pipeline",
    "app_metadata.services.pipelines.embeddings_msgs_pipeline",
    "app_metadata.services.pipelines.sentiment_classification_msg_pipeline",
    "app_telegram.services.pipelines.scan_pipeline",
    "app_telegram.services.pipelines.scan_comments_pipeline",
    "app_telegram.services.pipelines.access_room_pipeline",
    "app_telegram.services.pipelines.populate_pipeline",
    "app_telegram.services.pipelines.index_msgs_pipeline",
}


def backfill_trigger_context(apps, schema_editor):
    """Make pre-existing public Beat rows participate in the new policy."""

    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for trigger in PeriodicTask.objects.filter(task__in=SCHEDULED_TASKS).select_related(
        "interval", "crontab", "clocked"
    ):
        try:
            kwargs = json.loads(trigger.kwargs or "{}")
        except (TypeError, ValueError):
            kwargs = {}
        if not isinstance(kwargs, dict) or "scheduler_context" in kwargs:
            continue

        if trigger.interval_id:
            schedule = {
                "type": "interval",
                "every": trigger.interval.every,
                "period": trigger.interval.period,
            }
        elif trigger.crontab_id:
            schedule = {
                "type": "crontab",
                "minute": trigger.crontab.minute,
                "hour": trigger.crontab.hour,
                "day_of_week": trigger.crontab.day_of_week,
                "day_of_month": trigger.crontab.day_of_month,
                "month_of_year": trigger.crontab.month_of_year,
                "timezone": str(trigger.crontab.timezone),
            }
        elif trigger.clocked_id:
            schedule = {
                "type": "clocked",
                "clocked_time": trigger.clocked.clocked_time.isoformat(),
            }
        else:
            schedule = {}

        kwargs["scheduler_context"] = {
            "auto_finalize_failed": True,
            "replace_running_job": False,
            "retry_grace_seconds": 300,
            "scheduled": True,
            "trigger_id": trigger.pk,
            "schedule": schedule,
            "backfilled": True,
        }
        trigger.kwargs = json.dumps(kwargs, cls=app_base.models.GeneralEncoder)
        trigger.save(update_fields=["kwargs"])


def reverse_trigger_context(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for trigger in PeriodicTask.objects.filter(task__in=SCHEDULED_TASKS):
        try:
            kwargs = json.loads(trigger.kwargs or "{}")
        except (TypeError, ValueError):
            continue
        context = kwargs.get("scheduler_context")
        if isinstance(context, dict) and context.get("backfilled"):
            kwargs.pop("scheduler_context", None)
            trigger.kwargs = json.dumps(kwargs, cls=app_base.models.GeneralEncoder)
            trigger.save(update_fields=["kwargs"])


class Migration(migrations.Migration):

    dependencies = [
        ("django_celery_beat", "0018_improve_crontab_helptext"),
        ("app_scheduler", "0005_processingjob_lifecycle"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingjob",
            name="lifecycle_policy",
            field=models.JSONField(
                default=dict,
                encoder=app_base.models.GeneralEncoder,
            ),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="retry_deadline_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_trigger_context, reverse_trigger_context),
    ]
