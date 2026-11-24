import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("app_telegram", "0043_message_inputs_readonly")]

    operations = [migrations.CreateModel(
        name="EntityAnalyticsOutbox",
        fields=[
            ("id", models.BigAutoField(primary_key=True, serialize=False)),
            ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ("blocks", models.JSONField(default=list)),
            ("next_block", models.PositiveIntegerField(default=0)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
        ],
    )]
