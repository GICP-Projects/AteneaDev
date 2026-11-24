from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_entity", "0005_nerbatchresult"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="annotatedentity",
            index=models.Index(
                fields=["content_type", "object_id"],
                name="annotated_ct_object_idx",
            ),
        ),
    ]
