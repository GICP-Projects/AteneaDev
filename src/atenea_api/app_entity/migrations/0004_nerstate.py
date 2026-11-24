# Generated manually for the generic annotatable-object NER state.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_entity", "0003_alter_entityitem_type"),
        ("app_scheduler", "0001_processingjob_processingbatch"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="NERState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("object_id", models.UUIDField()),
                ("processed_input_hash", models.CharField(blank=True, default="", max_length=64)),
                ("claimed_input_hash", models.CharField(blank=True, default="", max_length=64)),
                ("ner_version", models.CharField(blank=True, default="", max_length=64)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("claimed", "Claimed"), ("extracting", "Extracting"), ("extracted", "Extracted"), ("storing", "Storing"), ("stored", "Stored"), ("completed", "Completed"), ("retry_wait", "Waiting to retry"), ("failed", "Failed")], default="claimed", max_length=16)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("retry_after", models.DateTimeField(blank=True, null=True)),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("last_error", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("claimed_batch", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="claimed_ner_states", to="app_scheduler.processingbatch")),
                ("content_type", models.ForeignKey(on_delete=models.deletion.CASCADE, to="contenttypes.contenttype")),
                ("last_batch", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="last_ner_state_batches", to="app_scheduler.processingbatch")),
                ("last_job", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="last_ner_states", to="app_scheduler.processingjob")),
            ],
        ),
        migrations.AddConstraint(
            model_name="nerstate",
            constraint=models.UniqueConstraint(fields=("content_type", "object_id"), name="unique_ner_state_content_object"),
        ),
        migrations.AddIndex(
            model_name="nerstate",
            index=models.Index(fields=["status", "lease_until"], name="nerstate_status_lease_idx"),
        ),
        migrations.AddIndex(
            model_name="nerstate",
            index=models.Index(fields=["ner_version", "processed_at"], name="nerstate_version_done_idx"),
        ),
    ]
