from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("app_telegram", "0044_entityanalyticsoutbox")]

    operations = [
        migrations.AddField(
            model_name="roomitem", name="username_history",
            field=models.JSONField(default=list, blank=True, editable=False),
        ),
        migrations.AddField(
            model_name="roomitem", name="username_checked_at",
            field=models.DateTimeField(null=True, blank=True, editable=False),
        ),
        # Older workers may still insert rooms during deployment. Django 4.2
        # otherwise drops the database default after adding this NOT NULL field.
        migrations.RunSQL(
            "ALTER TABLE app_telegram_roomitem ALTER COLUMN username_history SET DEFAULT '[]'::jsonb",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name="roomitem",
            name="metadata_sync_pending_at",
            field=models.DateTimeField(null=True, blank=True, editable=False),
        ),
        migrations.AddField(
            model_name="roomitem",
            name="metadata_sync_pending_fields",
            field=ArrayField(
                models.CharField(max_length=32), default=list, blank=True,
                editable=False, size=None,
            ),
        ),
        # Existing pending timestamps without a field list receive one full
        # synchronization. Keep the insert default for older workers as well.
        migrations.RunSQL(
            "ALTER TABLE app_telegram_roomitem ALTER COLUMN metadata_sync_pending_fields "
            "SET DEFAULT '{}'::varchar(32)[]",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
