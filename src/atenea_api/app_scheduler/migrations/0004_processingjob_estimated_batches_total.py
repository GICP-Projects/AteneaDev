# Generated manually for the durable processing estimate.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_scheduler", "0003_remove_processingbatch_working_payload"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingjob",
            name="estimated_batches_total",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
    ]
