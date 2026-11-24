"""Verify the schema installed by the clean entity migration history."""

import pytest
from django.db import connection


@pytest.mark.django_db
def test_entity_schema_keeps_numeric_types_and_source_indexes_without_count_table():
    tables = connection.introspection.table_names()
    assert "app_entity_itementitycount" not in tables
    assert "app_entity_annotatedentity" in tables
    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(cursor, "app_entity_entityitem")
        field = next(column for column in columns if column.name == "type")
        assert connection.introspection.get_field_type(field.type_code, field) == "SmallIntegerField"
        entity_indexes = connection.introspection.get_constraints(cursor, "app_entity_entityitem")
        annotation_indexes = connection.introspection.get_constraints(cursor, "app_entity_annotatedentity")
    assert entity_indexes["entity_type_id_idx"]["columns"] == ["type", "id"]
    assert annotation_indexes["annotated_ct_object_idx"]["columns"] == ["content_type_id", "object_id"]
