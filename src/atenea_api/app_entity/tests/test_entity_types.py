from importlib import import_module
import uuid

import pytest
from django.conf import settings
from django.db import connection, connections, models, transaction
from django.db.migrations.state import ModelState, ProjectState

from app_entity.enums import EntityType
from app_entity.models import EntityItem


TYPE_NAMES = (
    "ORG", "GPE", "PER", "EVENT", "PRODUCT", "NORP", "LAW", "LANGUAGE",
    "LOCATION", "MONEY", "DATE", "MISC", "EMAIL", "URL", "MENTION",
    "HASHTAG", "EMOJI", "WALLET_ETH", "WALLET_BTC", "WALLET_DASH",
    "WALLET_XMR",
)


@pytest.mark.parametrize(("code", "name"), enumerate(TYPE_NAMES, start=1))
def test_entity_type_mapping_is_stable(code, name):
    assert EntityType.from_name(name) == code
    assert EntityType.from_name(code) == code
    assert EntityType.to_name(code) == name


def test_entity_item_exposes_text_name_and_accepts_text_assignment():
    entity = EntityItem(name="Alice Example", unique_ent="aliceexample_per", type=EntityType.PER)

    assert EntityItem.PER == "PER"
    assert entity.type == EntityType.PER
    assert entity.type_name == "PER"

    entity.type_name = "url"
    assert entity.type == EntityType.URL
    assert entity.type_name == "URL"


def test_to_unique_accepts_text_names_and_numeric_codes():
    assert EntityItem.to_unique("Päblo Pérez", "PER") == "pabloperez_per"
    assert EntityItem.to_unique("Päblo Pérez", EntityType.PER) == "pabloperez_per"
    assert EntityItem.to_unique("Päblo Pérez", "3") == "pabloperez_per"


def _legacy_entity_state():
    state = ProjectState()
    state.add_model(ModelState(
        app_label="app_entity",
        name="EntityItem",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("type", models.CharField(max_length=15)),
        ],
    ))
    return state


def _sqlite_test_connection():
    alias = f"entity_type_migration_test_{uuid.uuid4().hex}"
    database_settings = dict(settings.DATABASES["default"])
    database_settings.update({
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "OPTIONS": {},
    })
    connections.databases[alias] = database_settings
    return alias, connections[alias]


def test_type_migration_converts_and_reverses_data_on_sqlite(django_db_blocker):
    migration = import_module("app_entity.migrations.0007_entity_type_codes")
    state = _legacy_entity_state()
    apps = state.apps
    model = apps.get_model("app_entity", "EntityItem")
    alias, connection = _sqlite_test_connection()
    try:
        with django_db_blocker.unblock(), connection.schema_editor() as schema_editor:
            schema_editor.create_model(model)
            with connection.cursor() as cursor:
                cursor.executemany(
                    'INSERT INTO "app_entity_entityitem" ("type") VALUES (%s)',
                    [(name,) for name in TYPE_NAMES],
                )

            migration.forwards(apps, schema_editor)
            with connection.cursor() as cursor:
                cursor.execute('SELECT "type" FROM "app_entity_entityitem" ORDER BY "id"')
                assert [row[0] for row in cursor.fetchall()] == list(range(1, 22))

            migration.backwards(apps, schema_editor)
            with connection.cursor() as cursor:
                cursor.execute('SELECT "type" FROM "app_entity_entityitem" ORDER BY "id"')
                assert [row[0] for row in cursor.fetchall()] == list(TYPE_NAMES)
    finally:
        connection.close()
        del connections.databases[alias]


def test_type_migration_rejects_unknown_labels_before_changes(django_db_blocker):
    migration = import_module("app_entity.migrations.0007_entity_type_codes")
    state = _legacy_entity_state()
    apps = state.apps
    model = apps.get_model("app_entity", "EntityItem")
    alias, connection = _sqlite_test_connection()
    try:
        with django_db_blocker.unblock(), connection.schema_editor() as schema_editor:
            schema_editor.create_model(model)
            with connection.cursor() as cursor:
                cursor.execute(
                    'INSERT INTO "app_entity_entityitem" ("type") VALUES (%s)',
                    ["ALIEN"],
                )

            with pytest.raises(RuntimeError, match="unknown values found"):
                migration.forwards(apps, schema_editor)

            with connection.cursor() as cursor:
                cursor.execute('SELECT "type" FROM "app_entity_entityitem"')
                assert cursor.fetchone()[0] == "ALIEN"
    finally:
        connection.close()
        del connections.databases[alias]


@pytest.mark.django_db(transaction=True)
def test_type_migration_converts_and_reverses_postgresql_smallint():
    migration = import_module("app_entity.migrations.0007_entity_type_codes")
    with transaction.atomic():
        # A temporary table shadows the model table; the real test data stays untouched.
        with connection.cursor() as cursor:
            cursor.execute(
                'CREATE TEMP TABLE "app_entity_entityitem" '
                '("id" serial, "type" varchar(15) NOT NULL) ON COMMIT DROP'
            )
            cursor.executemany(
                'INSERT INTO "app_entity_entityitem" ("type") VALUES (%s)',
                [(name,) for name in TYPE_NAMES],
            )
        with connection.schema_editor(atomic=False) as schema_editor:
            migration.forwards(_legacy_entity_state().apps, schema_editor)
            with connection.cursor() as cursor:
                cursor.execute('SELECT "type", pg_typeof("type")::text '
                               'FROM "app_entity_entityitem" ORDER BY "id"')
                assert cursor.fetchall() == [(code, "smallint") for code in range(1, 22)]
            migration.backwards(_legacy_entity_state().apps, schema_editor)
            with connection.cursor() as cursor:
                cursor.execute('SELECT "type" FROM "app_entity_entityitem" ORDER BY "id"')
                assert [row[0] for row in cursor.fetchall()] == list(TYPE_NAMES)
