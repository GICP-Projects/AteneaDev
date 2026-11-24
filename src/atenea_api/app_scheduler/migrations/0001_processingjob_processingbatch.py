# Generated manually for the generic durable processing scheduler.

import app_base.models
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ProcessingJob",
            fields=[
                ("id", models.UUIDField(default=__import__("uuid").uuid4, editable=False, primary_key=True, serialize=False)),
                ("processor", models.CharField(max_length=64)),
                ("mode", models.CharField(choices=[("live", "Live"), ("backfill", "Backfill")], max_length=16)),
                ("workflow", models.JSONField(default=list, encoder=app_base.models.GeneralEncoder)),
                ("status", models.CharField(choices=[("collecting", "Collecting input"), ("running", "Running"), ("paused", "Paused"), ("cancelling", "Cancelling"), ("cancelled", "Cancelled"), ("completed", "Completed"), ("failed", "Failed")], default="collecting", max_length=16)),
                ("filters", models.JSONField(default=dict, encoder=app_base.models.GeneralEncoder)),
                ("source_snapshot_at", models.DateTimeField(blank=True, null=True)),
                ("source_cursor", models.JSONField(default=dict, encoder=app_base.models.GeneralEncoder)),
                ("source_exhausted", models.BooleanField(default=False)),
                ("input_closed_at", models.DateTimeField(blank=True, null=True)),
                ("fingerprint", models.CharField(db_index=True, max_length=64)),
                ("batch_size", models.PositiveIntegerField(default=500)),
                ("max_inflight_batches", models.PositiveSmallIntegerField(default=1)),
                ("total_items", models.PositiveBigIntegerField(default=0)),
                ("processed_items", models.PositiveBigIntegerField(default=0)),
                ("failed_items", models.PositiveBigIntegerField(default=0)),
                ("total_batches", models.PositiveIntegerField(default=0)),
                ("completed_batches", models.PositiveIntegerField(default=0)),
                ("failed_batches", models.PositiveIntegerField(default=0)),
                ("request_token", models.UUIDField(blank=True, db_index=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="ProcessingBatch",
            fields=[
                ("id", models.UUIDField(default=__import__("uuid").uuid4, editable=False, primary_key=True, serialize=False)),
                ("ordinal", models.PositiveIntegerField()),
                ("status", models.CharField(choices=[("pending", "Ready"), ("running", "Running a stage"), ("retry_wait", "Waiting to retry"), ("completed", "Completed"), ("failed", "Failed"), ("cancelled", "Cancelled")], default="pending", max_length=16)),
                ("item_ids", models.JSONField(default=list, encoder=app_base.models.GeneralEncoder)),
                ("item_count", models.PositiveIntegerField(default=0)),
                ("current_stage", models.CharField(blank=True, default="", max_length=128)),
                ("completed_stages", models.JSONField(default=list, encoder=app_base.models.GeneralEncoder)),
                ("stage_attempts", models.JSONField(default=dict, encoder=app_base.models.GeneralEncoder)),
                ("working_payload", models.JSONField(blank=True, encoder=app_base.models.GeneralEncoder, null=True)),
                ("processor_metadata", models.JSONField(default=dict, encoder=app_base.models.GeneralEncoder)),
                ("processor_version", models.CharField(blank=True, default="", max_length=64)),
                ("processed_count", models.PositiveIntegerField(default=0)),
                ("failed_count", models.PositiveIntegerField(default=0)),
                ("queued_at", models.DateTimeField(blank=True, null=True)),
                ("lease_token", models.CharField(blank=True, default="", max_length=64)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("run_after", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("job", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="batches", to="app_scheduler.processingjob")),
            ],
            options={"ordering": ["job_id", "ordinal"]},
        ),
        migrations.AddIndex(
            model_name="processingjob",
            index=models.Index(fields=["processor", "status", "created_at"], name="app_schedul_process_45ff23_idx"),
        ),
        migrations.AddIndex(
            model_name="processingjob",
            index=models.Index(fields=["mode", "status", "created_at"], name="app_schedul_mode_b0b576_idx"),
        ),
        migrations.AddConstraint(
            model_name="processingjob",
            constraint=models.UniqueConstraint(condition=Q(("status__in", ("collecting", "running", "paused", "cancelling"))), fields=("processor", "fingerprint"), name="scheduler_active_processor_fingerprint"),
        ),
        migrations.AddConstraint(
            model_name="processingbatch",
            constraint=models.UniqueConstraint(fields=("job", "ordinal"), name="scheduler_job_batch_ordinal"),
        ),
        migrations.AddIndex(
            model_name="processingbatch",
            index=models.Index(fields=["job", "status", "ordinal"], name="app_schedul_job_id_8bf0c6_idx"),
        ),
        migrations.AddIndex(
            model_name="processingbatch",
            index=models.Index(fields=["status", "queued_at"], name="app_schedul_status_aecfa3_idx"),
        ),
        migrations.AddIndex(
            model_name="processingbatch",
            index=models.Index(fields=["lease_until"], name="app_schedul_lease_u_0e44ee_idx"),
        ),
    ]
