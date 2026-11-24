# Generated manually for durable metadata processing.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    # EmbeddingsItem already exists on production databases and can be very
    # large.  Its new operational lookup index must not block writers.
    atomic = False

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("app_scheduler", "0002_processinghandoff_processingbatch_deferred_count_and_more"),
        ("app_metadata", "0004_refactor_embeddingsitem_vector_storage"),
    ]

    operations = [
        migrations.AddField(
            model_name="embeddingsitem",
            name="attempt_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="embeddingsitem",
            name="claimed_batch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="claimed_embedding_states", to="app_scheduler.processingbatch"),
        ),
        migrations.AddField(
            model_name="embeddingsitem",
            name="last_batch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="last_embedding_state_batches", to="app_scheduler.processingbatch"),
        ),
        migrations.AddField(
            model_name="embeddingsitem",
            name="last_job",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="last_embedding_states", to="app_scheduler.processingjob"),
        ),
        migrations.AddField(
            model_name="embeddingsitem",
            name="lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="embeddingsitem",
            name="processing_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="embeddingsitem",
            name="processing_status",
            field=models.CharField(choices=[("pending", "Pending"), ("claimed", "Claimed"), ("running", "Running"), ("retry_wait", "Waiting to retry"), ("completed", "Completed"), ("failed", "Failed")], default="pending", max_length=16),
        ),
        migrations.AddField(
            model_name="embeddingsitem",
            name="retry_after",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS embedstate_status_lease_idx "
                        "ON app_metadata_embeddingsitem (processing_status, lease_until);"
                    ),
                    reverse_sql="DROP INDEX CONCURRENTLY IF EXISTS embedstate_status_lease_idx;",
                )
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="embeddingsitem",
                    index=models.Index(
                        fields=["processing_status", "lease_until"],
                        name="embedstate_status_lease_idx",
                    ),
                )
            ],
        ),
        migrations.CreateModel(
            name="SentimentState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("object_id", models.UUIDField()),
                ("processed_input_hash", models.CharField(blank=True, default="", max_length=64)),
                ("claimed_input_hash", models.CharField(blank=True, default="", max_length=64)),
                ("sentiment_version", models.CharField(blank=True, default="", max_length=256)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("claimed", "Claimed"), ("running", "Running"), ("retry_wait", "Waiting to retry"), ("completed", "Completed"), ("failed", "Failed")], default="pending", max_length=16)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("retry_after", models.DateTimeField(blank=True, null=True)),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("last_error", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("claimed_batch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="claimed_sentiment_states", to="app_scheduler.processingbatch")),
                ("content_type", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="contenttypes.contenttype")),
                ("last_batch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="last_sentiment_state_batches", to="app_scheduler.processingbatch")),
                ("last_job", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="last_sentiment_states", to="app_scheduler.processingjob")),
            ],
        ),
        migrations.AddConstraint(
            model_name="sentimentstate",
            constraint=models.UniqueConstraint(fields=("content_type", "object_id"), name="unique_sentiment_state_content_object"),
        ),
        migrations.AddIndex(
            model_name="sentimentstate",
            index=models.Index(fields=["status", "lease_until"], name="sentstate_status_lease_idx"),
        ),
        migrations.AddIndex(
            model_name="sentimentstate",
            index=models.Index(fields=["sentiment_version", "processed_at"], name="sentstate_version_done_idx"),
        ),
    ]
