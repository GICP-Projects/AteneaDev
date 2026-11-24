"""Policy shared by public Beat triggers and durable ProcessingJobs.

The public scheduler stores its options in the Beat task kwargs under a
reserved key.  Application-owned pipeline launchers receive that context and
snapshot it on the root ProcessingJob.  Keeping the snapshot on the Job is
important: editing a schedule must not change the lifecycle of a run that is
already in progress.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from django_celery_beat.models import PeriodicTask
from django_celery_beat.tzcrontab import TzAwareCrontab


SCHEDULER_CONTEXT_KWARG = "scheduler_context"
LIFECYCLE_FIELDS = (
    "auto_finalize_failed",
    "replace_running_job",
    "retry_grace_seconds",
)
DEFAULT_LIFECYCLE_OPTIONS = {
    "auto_finalize_failed": True,
    "replace_running_job": False,
    # Five minutes is a safety margin before the next scheduled handoff.  It
    # is capped relative to the actual cadence for short schedules.
    "retry_grace_seconds": 300,
}


def normalize_lifecycle_options(options=None) -> dict:
    """Return a validated, JSON-safe set of public lifecycle options."""

    options = options or {}
    if not isinstance(options, dict):
        raise ValueError("lifecycle options must be an object")

    normalized = dict(DEFAULT_LIFECYCLE_OPTIONS)
    for name in LIFECYCLE_FIELDS:
        if name in options:
            normalized[name] = options[name]
    normalized["auto_finalize_failed"] = bool(normalized["auto_finalize_failed"])
    normalized["replace_running_job"] = bool(normalized["replace_running_job"])
    try:
        normalized["retry_grace_seconds"] = int(normalized["retry_grace_seconds"])
    except (TypeError, ValueError) as exc:
        raise ValueError("retry_grace_seconds must be an integer") from exc
    if normalized["retry_grace_seconds"] < 1:
        raise ValueError("retry_grace_seconds must be positive")
    return normalized


def public_lifecycle_options(context=None) -> dict:
    """Return only the options exposed by the trigger API/dashboard."""

    context = context if isinstance(context, dict) else {}
    return {
        name: context.get(name, default)
        for name, default in DEFAULT_LIFECYCLE_OPTIONS.items()
    }


def extract_scheduler_context(kwargs) -> tuple[dict, dict]:
    """Split internal scheduler context from public Celery kwargs."""

    if not isinstance(kwargs, dict):
        return {}, {}
    public_kwargs = dict(kwargs)
    context = public_kwargs.pop(SCHEDULER_CONTEXT_KWARG, {})
    return public_kwargs, context if isinstance(context, dict) else {}


def _schedule_snapshot(trigger: PeriodicTask) -> dict:
    if trigger.interval_id:
        interval = trigger.interval
        return {
            "type": "interval",
            "every": interval.every,
            "period": interval.period,
        }
    if trigger.crontab_id:
        crontab = trigger.crontab
        return {
            "type": "crontab",
            "minute": crontab.minute,
            "hour": crontab.hour,
            "day_of_week": crontab.day_of_week,
            "day_of_month": crontab.day_of_month,
            "month_of_year": crontab.month_of_year,
            "timezone": str(crontab.timezone),
        }
    if trigger.clocked_id:
        return {
            "type": "clocked",
            "clocked_time": trigger.clocked.clocked_time.isoformat(),
        }
    return {}


def build_scheduler_context(trigger: PeriodicTask, options=None) -> dict:
    """Build the hidden kwargs sent by django-celery-beat to a launcher."""

    context = normalize_lifecycle_options(options)
    context.update(
        {
            "scheduled": True,
            "trigger_id": trigger.pk,
            "schedule": _schedule_snapshot(trigger),
        }
    )
    return context


def _period_seconds(period: str, every: int) -> int:
    try:
        return int(timedelta(**{period: every}).total_seconds())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid interval period: {period!r}") from exc


def max_retry_grace_seconds_for_interval(every: int, period: str) -> int:
    """Return the largest safety margin allowed for an interval schedule."""

    cadence_seconds = _period_seconds(period, int(every))
    return max(int(cadence_seconds // 4), 1)


def _schedule_from_snapshot(snapshot: dict):
    schedule_type = snapshot.get("type")
    if schedule_type == "interval":
        seconds = _period_seconds(snapshot.get("period"), int(snapshot.get("every")))
        return seconds, None
    if schedule_type == "crontab":
        try:
            tz = ZoneInfo(snapshot.get("timezone") or settings.TIME_ZONE)
        except Exception:  # pragma: no cover - invalid legacy data is best effort
            tz = timezone.get_current_timezone()
        return None, TzAwareCrontab(
            minute=snapshot.get("minute", "*"),
            hour=snapshot.get("hour", "*"),
            day_of_week=snapshot.get("day_of_week", "*"),
            day_of_month=snapshot.get("day_of_month", "*"),
            month_of_year=snapshot.get("month_of_year", "*"),
            tz=tz,
        )
    return None, None


def _next_scheduled_run(context: dict, now):
    """Return the next recurring Beat fire, or ``None`` for one-off jobs."""

    if not context.get("scheduled"):
        return None

    trigger = None
    trigger_id = context.get("trigger_id")
    if trigger_id:
        try:
            trigger = PeriodicTask.objects.select_related(
                "interval", "crontab", "clocked"
            ).get(pk=trigger_id)
        except (PeriodicTask.DoesNotExist, TypeError, ValueError):
            trigger = None

    if trigger is not None:
        if not trigger.enabled or trigger.clocked_id:
            return None
        if trigger.interval_id:
            cadence = timedelta(
                seconds=_period_seconds(trigger.interval.period, trigger.interval.every)
            )
            base = trigger.last_run_at or now
            next_run = base + cadence
            # Beat may have delivered one or more ticks while the equivalent
            # Job was still RUNNING.  Keep the next deadline on the next real
            # slot, not ``failure_time + interval``.
            while next_run <= now:
                next_run += cadence
            return next_run
        if trigger.crontab_id:
            schedule = trigger.crontab.schedule
            base = trigger.last_run_at or now
            estimate = schedule.remaining_estimate(base)
            next_run = base + estimate
            if next_run <= now:
                estimate = schedule.remaining_estimate(now)
                next_run = now + estimate
            if next_run <= now:
                next_run = now + timedelta(seconds=1)
            return next_run

    snapshot = context.get("schedule")
    if not isinstance(snapshot, dict) or snapshot.get("type") == "clocked":
        return None
    seconds, crontab = _schedule_from_snapshot(snapshot)
    if seconds is not None:
        return now + timedelta(seconds=seconds)
    if crontab is not None:
        estimate = crontab.remaining_estimate(now)
        if estimate.total_seconds() <= 0:
            estimate = timedelta(seconds=1)
        return now + estimate
    return None


def retry_deadline_for(context: dict, now=None):
    """Compute the deadline immediately before the next scheduled launch."""

    context = context if isinstance(context, dict) else {}
    if not context.get("scheduled") or not context.get("auto_finalize_failed", True):
        return None
    now = now or timezone.now()
    next_run = _next_scheduled_run(context, now)
    if next_run is None:
        return None

    requested_grace = int(
        context.get("retry_grace_seconds", DEFAULT_LIFECYCLE_OPTIONS["retry_grace_seconds"])
    )
    cadence = max((next_run - now).total_seconds(), 1)
    # Leave room for a short schedule to actually retry, while preserving the
    # configured five-minute margin for normal hourly/daily schedules.
    effective_grace = min(requested_grace, max(int(cadence // 4), 1))
    return max(now, next_run - timedelta(seconds=effective_grace))
