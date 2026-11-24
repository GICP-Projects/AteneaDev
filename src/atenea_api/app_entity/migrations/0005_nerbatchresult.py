import app_base.models
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("app_entity", "0004_nerstate"),
        ("app_scheduler", "0002_processinghandoff_processingbatch_deferred_count_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="NERBatchResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("payload", models.JSONField(encoder=app_base.models.GeneralEncoder)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="ner_result", to="app_scheduler.processingbatch")),
            ],
        ),
    ]
