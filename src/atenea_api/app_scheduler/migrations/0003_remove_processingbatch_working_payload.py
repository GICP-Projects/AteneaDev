from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("app_scheduler", "0002_processinghandoff_processingbatch_deferred_count_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="processingbatch",
            name="working_payload",
        ),
    ]
