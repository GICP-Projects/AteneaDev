# Generated manually: the MessageItem cursor index is concurrent in production.

from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("app_telegram", "0037_add_deleted_downloadable_status"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS msg_stored_date_id_idx "
                        "ON app_telegram_messageitem (stored_date, id);"
                    ),
                    reverse_sql="DROP INDEX CONCURRENTLY IF EXISTS msg_stored_date_id_idx;",
                )
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="messageitem",
                    index=models.Index(fields=["stored_date", "id"], name="msg_stored_date_id_idx"),
                )
            ],
        ),
    ]
