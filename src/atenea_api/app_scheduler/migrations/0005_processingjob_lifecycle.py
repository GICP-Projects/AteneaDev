from django.db import migrations, models
from django.db.models import Q


def move_legacy_retryable_jobs(apps, schema_editor):
    """Undo the old terminal transition for rows with failed batches.

    Before ``WAITING_RETRY`` existed, a failed source batch could make its
    parent look completed and seal descendants.  Keep all batch rows intact,
    but reopen the input boundary so a later retry can deliver its missing
    handoffs.
    """

    ProcessingJob = apps.get_model("app_scheduler", "ProcessingJob")
    ProcessingBatch = apps.get_model("app_scheduler", "ProcessingBatch")

    failed_job_ids = ProcessingBatch.objects.filter(status="failed").values_list("job_id", flat=True)
    legacy_jobs = list(
        ProcessingJob.objects.filter(
            pk__in=failed_job_ids,
            status__in=("completed", "failed"),
        ).values_list("pk", flat=True)
    )
    for job_id in legacy_jobs:
        ProcessingJob.objects.filter(pk=job_id).update(
            status="waiting_retry",
            source_finished_at=None,
            completed_at=None,
        )

    # Reopen every descendant that was sealed by the incorrect parent
    # transition.  A descendant's own failed batches remain retryable too.
    frontier = legacy_jobs
    while frontier:
        children = list(
            ProcessingJob.objects.filter(
                parent_job_id__in=frontier,
                input_closed_at__isnull=False,
            ).values_list("pk", flat=True)
        )
        if not children:
            break
        for child_id in children:
            has_failed_batch = ProcessingBatch.objects.filter(
                job_id=child_id,
                status="failed",
            ).exists()
            has_batches = ProcessingBatch.objects.filter(job_id=child_id).exists()
            ProcessingJob.objects.filter(pk=child_id).update(
                status="waiting_retry" if has_failed_batch else ("running" if has_batches else "collecting"),
                input_closed_at=None,
                source_finished_at=None,
                completed_at=None,
            )
        frontier = children


class Migration(migrations.Migration):

    dependencies = [
        ("app_scheduler", "0004_processingjob_estimated_batches_total"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="processingjob",
            name="scheduler_active_processor_fingerprint",
        ),
        migrations.AddField(
            model_name="processingjob",
            name="failures_abandoned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="input_outcome",
            field=models.CharField(
                choices=[("complete", "Complete"), ("partial", "Partial")],
                default="complete",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="processingjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("collecting", "Collecting input"),
                    ("running", "Running"),
                    ("paused", "Paused"),
                    ("cancelling", "Cancelling"),
                    ("cancelled", "Cancelled"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("waiting_retry", "Waiting for retry"),
                    ("partial", "Partial"),
                ],
                default="collecting",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="processingjob",
            constraint=models.UniqueConstraint(
                condition=Q(
                    status__in=(
                        "collecting", "running", "paused", "cancelling", "waiting_retry",
                    )
                ),
                fields=("processor", "fingerprint"),
                name="scheduler_active_processor_fingerprint",
            ),
        ),
        migrations.RunPython(move_legacy_retryable_jobs, migrations.RunPython.noop),
    ]
