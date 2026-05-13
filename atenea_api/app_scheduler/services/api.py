from app_base.api import create_advance_filter
from django_celery_beat.models import (
    PeriodicTask,
    IntervalSchedule,
    ClockedSchedule,
    CrontabSchedule
)



# ==================================================================
#               Scheduler endpoints handlers
# ==================================================================

def search(
    task = None,
    name = None,
    description = None,
    schedule_type = None
):
    """Search for scheduled tasks.

    Parameters
    ----------
    task: str, default=None
        Scheduled task.

    name: str, default=None
        Name for this scheduling.

    description: str, default=None
        Description for this scheduling.

    schedule_type: str, default=None
        Filter scheduled tasks by the scheduling method ('interval' or 'crontab').

    Returns
    -------
    queryset: QuerySet[PeriodicTask]
        A queryset of scheduled tasks.
    """
    return PeriodicTask.objects.filter(
        create_advance_filter(
            and_filter_fields={
                "task__iexact": task,
                "name__icontains": name,
                "description__icontains": description,
                # None to ignore this filter
                f"{schedule_type}__isnull": False if schedule_type else None
            }
        )
    ).order_by('name')
