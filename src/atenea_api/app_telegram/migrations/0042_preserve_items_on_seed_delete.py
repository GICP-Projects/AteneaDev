from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("app_telegram", "0041_seeditem_seed_tags_gin_idx"),
    ]

    operations = [
        migrations.AlterField(
            model_name="roomitem",
            name="seed_item",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="%(class)ss_related",
                to="app_telegram.seeditem",
            ),
        ),
        migrations.AlterField(
            model_name="useritem",
            name="seed_item",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="%(class)ss_related",
                to="app_telegram.seeditem",
            ),
        ),
    ]
