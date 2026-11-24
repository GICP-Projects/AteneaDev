from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_telegram", "0038_messageitem_stored_date_id_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="messageitem",
            name="ner_input_hash",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
    ]
