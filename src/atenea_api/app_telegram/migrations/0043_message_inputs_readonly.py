from django.db import migrations, models

from app_base.models import LanguageField


class Migration(migrations.Migration):

    dependencies = [
        ("app_telegram", "0042_preserve_items_on_seed_delete"),
    ]

    operations = [
        migrations.AlterField(
            model_name="messageitem",
            name="text",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="messageitem",
            name="annotated_text",
            field=models.TextField(
                blank=True,
                editable=False,
                null=True,
                help_text=(
                    "Contains a cleaned version of the text. Entities start/end "
                    "offsets point to this field (That's why it is 'annotated'"
                ),
            ),
        ),
        migrations.AlterField(
            model_name="messageitem",
            name="lang",
            field=LanguageField(blank=True, editable=False),
        ),
    ]
