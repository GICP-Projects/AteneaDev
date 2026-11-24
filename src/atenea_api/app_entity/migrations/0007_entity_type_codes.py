"""Store entity types as stable numeric codes while preserving their names."""

from django.db import migrations, models


# Frozen in migration history. Keep these codes aligned with EntityType.
TYPE_CODES = (
    (1, "ORG"),
    (2, "GPE"),
    (3, "PER"),
    (4, "EVENT"),
    (5, "PRODUCT"),
    (6, "NORP"),
    (7, "LAW"),
    (8, "LANGUAGE"),
    (9, "LOCATION"),
    (10, "MONEY"),
    (11, "DATE"),
    (12, "MISC"),
    (13, "EMAIL"),
    (14, "URL"),
    (15, "MENTION"),
    (16, "HASHTAG"),
    (17, "EMOJI"),
    (18, "WALLET_ETH"),
    (19, "WALLET_BTC"),
    (20, "WALLET_DASH"),
    (21, "WALLET_XMR"),
)

TYPE_CHOICES = (
    (1, "Companies, agencies, institutions."),
    (2, "Geopolitical entity, i.e. countries, cities, states."),
    (3, "People, including fictional."),
    (4, "Named hurricanes, battles, wars, sports events, etc."),
    (5, "Objects, vehicles, foods, etc. (Not services.)"),
    (6, "Nationalities or religious or political groups."),
    (7, "Named documents made into laws."),
    (8, "Any named language."),
    (9, "Non-GPE locations, mountain ranges, bodies of water."),
    (10, "Monetary values, including unit."),
    (11, "Absolute or relative dates or periods."),
    (12, "Miscellaneous named entities."),
    (13, "Any type of email."),
    (14, "Any type of URL."),
    (15, "Standard social network mentions (@name)."),
    (16, "Standard social network hashtags (#hashtag)."),
    (17, "Any type of emoji."),
    (18, "Ethereum (ETH) wallet address."),
    (19, "Bitcoin (BTC) wallet address."),
    (20, "Dash (DASH) wallet address."),
    (21, "Monero (XMR) wallet address."),
)


def _assert_known_values(schema_editor, expected):
    quote = schema_editor.quote_name
    table = quote("app_entity_entityitem")
    column = quote("type")
    placeholders = ", ".join(["%s"] * len(expected))
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} NOT IN ({placeholders})",
            list(expected),
        )
        unknown = [row[0] for row in cursor.fetchall()]
    if unknown:
        raise RuntimeError(
            "Cannot convert app_entity_entityitem.type; unknown values found: "
            f"{unknown!r}"
        )


def _alter_entity_type(apps, schema_editor, *, reverse=False):
    connection = schema_editor.connection
    quote = schema_editor.quote_name
    table = quote("app_entity_entityitem")
    column = quote("type")
    codes = tuple(code for code, _ in TYPE_CODES)
    names = tuple(name for _, name in TYPE_CODES)

    if reverse:
        _assert_known_values(schema_editor, codes)
    else:
        _assert_known_values(schema_editor, names)

    if connection.vendor == "postgresql":
        if reverse:
            cases = " ".join(
                f"WHEN {code} THEN '{name}'" for code, name in TYPE_CODES
            )
            sql = (
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE varchar(15) "
                f"USING (CASE {column} {cases} END)::varchar(15)"
            )
        else:
            cases = " ".join(
                f"WHEN '{name}' THEN {code}" for code, name in TYPE_CODES
            )
            sql = (
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE smallint "
                f"USING CASE {column} {cases} END"
            )
        with connection.cursor() as cursor:
            if reverse:
                cursor.execute(f"ALTER TABLE {table} DROP CONSTRAINT {quote('entity_type_nonnegative')}")
            cursor.execute(sql)
            if not reverse:
                cursor.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT {quote('entity_type_nonnegative')} "
                    f"CHECK ({column} >= 0)"
                )
        return

    model = apps.get_model("app_entity", "EntityItem")
    if reverse:
        source_field = models.PositiveSmallIntegerField(choices=TYPE_CHOICES)
        target_field = models.CharField(
            max_length=15,
            choices=[
                (name, description)
                for (_, name), (_, description) in zip(TYPE_CODES, TYPE_CHOICES)
            ],
        )
        updates = ((code, name) for code, name in TYPE_CODES)
    else:
        source_field = model._meta.get_field("type")
        target_field = models.PositiveSmallIntegerField(choices=TYPE_CHOICES)
        updates = ((name, code) for code, name in TYPE_CODES)
    source_field.set_attributes_from_name("type")
    target_field.set_attributes_from_name("type")

    # SQLite keeps numeric values as text in the old varchar column. The
    # subsequent schema-editor rebuild casts them into the new column type.
    with connection.cursor() as cursor:
        for old_value, new_value in updates:
            cursor.execute(
                f"UPDATE {table} SET {column} = %s WHERE {column} = %s",
                [new_value, old_value],
            )

    schema_editor.alter_field(model, source_field, target_field, strict=True)


def forwards(apps, schema_editor):
    _alter_entity_type(apps, schema_editor)


def backwards(apps, schema_editor):
    _alter_entity_type(apps, schema_editor, reverse=True)


class Migration(migrations.Migration):
    dependencies = [
        ("app_entity", "0006_annotatedentity_content_type_object_idx"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(forwards, backwards)],
            state_operations=[
                migrations.AlterField(
                    model_name="entityitem",
                    name="type",
                    field=models.PositiveSmallIntegerField(choices=TYPE_CHOICES),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="entityitem",
            index=models.Index(fields=["type", "id"], name="entity_type_id_idx"),
        ),
    ]
