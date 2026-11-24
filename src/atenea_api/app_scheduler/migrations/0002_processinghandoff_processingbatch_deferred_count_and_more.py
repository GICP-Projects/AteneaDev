# Generated manually for the second durable-processing rollout.

import app_base.models
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("app_scheduler", "0001_processingjob_processingbatch"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProcessingHandoff",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("item_ids", models.JSONField(default=list, encoder=app_base.models.GeneralEncoder)),
                ("item_count", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("materialized", "Materialized"), ("cancelled", "Cancelled")], default="pending", max_length=16)),
                ("materialized_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddField(model_name="processingbatch", name="deferred_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="processingbatch", name="item_content_type", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="processing_batches", to="contenttypes.contenttype")),
        migrations.AddField(model_name="processingbatch", name="recover_on_lease_expiry", field=models.BooleanField(default=True)),
        migrations.AddField(model_name="processingbatch", name="resource_key", field=models.CharField(blank=True, default="", max_length=128)),
        migrations.AddField(model_name="processingbatch", name="result_summary", field=models.JSONField(default=dict, encoder=app_base.models.GeneralEncoder)),
        migrations.AddField(model_name="processingbatch", name="skipped_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="processingjob", name="deferred_items", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="processingjob", name="descendant_deferred_items", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="processingjob", name="descendant_failed_items", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="processingjob", name="descendant_processed_items", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="processingjob", name="descendant_skipped_items", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="processingjob", name="parent_job", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="child_jobs", to="app_scheduler.processingjob")),
        migrations.AddField(model_name="processingjob", name="skipped_items", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="processingjob", name="source_finished_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddIndex(model_name="processingbatch", index=models.Index(fields=["job", "resource_key", "status"], name="app_schedul_job_res_9d1590_idx")),
        migrations.AddIndex(model_name="processingjob", index=models.Index(fields=["parent_job", "status", "created_at"], name="app_schedul_parent_08bf4b_idx")),
        migrations.AddField(model_name="processinghandoff", name="item_content_type", field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="processing_handoffs", to="contenttypes.contenttype")),
        migrations.AddField(model_name="processinghandoff", name="source_batch", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="outgoing_handoffs", to="app_scheduler.processingbatch")),
        migrations.AddField(model_name="processinghandoff", name="target_batch", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="materialized_from_handoff", to="app_scheduler.processingbatch")),
        migrations.AddField(model_name="processinghandoff", name="target_job", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="incoming_handoffs", to="app_scheduler.processingjob")),
        migrations.AddField(model_name="processingbatch", name="source_handoff", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="materialized_batches", to="app_scheduler.processinghandoff")),
        migrations.AddIndex(model_name="processinghandoff", index=models.Index(fields=["target_job", "status", "created_at"], name="app_schedul_handof_1f2d51_idx")),
        migrations.AddIndex(model_name="processinghandoff", index=models.Index(fields=["source_batch", "target_job"], name="app_schedul_handof_4b0941_idx")),
    ]
