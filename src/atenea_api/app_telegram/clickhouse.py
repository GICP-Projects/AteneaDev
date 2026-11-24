"""ClickHouse schema and transport for exact, signed entity facts.

The index service serializes writers with a PostgreSQL advisory lock and
replays saved insert blocks with stable deduplication tokens.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import re
from uuid import UUID

import clickhouse_connect
from clickhouse_connect.driver.external import ExternalData
from django.conf import settings
from django.db import connections


TABLE = "entity_facts"
FACT_COLUMNS = (
    "msg_id", "entity_id", "room_id", "type", "created_at", "stored_date",
    "is_reply", "count",
)
INSERT_COLUMNS = (*FACT_COLUMNS, "sign", "version")


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
        raise ValueError(f"Invalid ClickHouse identifier: {value!r}")
    return f"`{value}`"


def table_name():
    return f"{identifier(settings.CLICKHOUSE['database'])}.{identifier(TABLE)}"


def query_with_ids(client, query, *, parameters, **kwargs):
    """Large UUID selections travel in POST data rather than an oversized URI."""
    parameters = dict(parameters)
    external = None
    for name in ("rooms", "entities", "ids"):
        values = parameters.get(name)
        marker = "{" + name + ":Array(UUID)}"
        if isinstance(values, (list, tuple)) and len(values) > 1000 and marker in query:
            external = ExternalData() if external is None else external
            external.add_file(file_name=f"atenea_{name}.tsv", fmt="TabSeparated", structure=["id UUID"],
                              data="\n".join(str(UUID(str(value))) for value in values).encode())
            query = query.replace(marker, f"(SELECT id FROM atenea_{name})")
            del parameters[name]
    if external is not None:
        kwargs["external_data"] = external
    return client.query(query, parameters=parameters, **kwargs)


def get_client(*, database=True):
    options = dict(settings.CLICKHOUSE)
    if not database:
        options["database"] = "default"
    return clickhouse_connect.get_client(
        **options, query_retries=0, query_limit=0,
        settings=settings.CLICKHOUSE_QUERY_SETTINGS,
    )


@contextmanager
def client_session(client=None):
    owned = client is None
    client = get_client() if owned else client
    try:
        yield client
    finally:
        if owned:
            client.close()


def ensure_schema():
    """Explicit startup operation; importing this module never creates tables."""
    client = get_client(database=False)
    try:
        client.command(f"CREATE DATABASE IF NOT EXISTS {identifier(settings.CLICKHOUSE['database'])}")
        client.command(f"""
            CREATE TABLE IF NOT EXISTS {table_name()} (
                msg_id UUID, entity_id UUID, room_id UUID, type Int16,
                created_at Nullable(DateTime64(6, 'UTC')),
                stored_date Nullable(DateTime64(6, 'UTC')),
                is_reply Bool, count Int64,
                sign Int8, version UInt64,
                CONSTRAINT valid_sign CHECK sign IN (-1, 1),
                CONSTRAINT valid_count CHECK count > 0
            ) ENGINE = VersionedCollapsingMergeTree(sign, version)
            PARTITION BY toYYYYMM(ifNull(created_at, toDateTime64('1970-01-01', 6, 'UTC')))
            ORDER BY (msg_id, entity_id)
            SETTINGS non_replicated_deduplication_window = {settings.CLICKHOUSE_DEDUPLICATION_WINDOW}
        """)
        engine, definition = client.query(
            "SELECT engine, create_table_query FROM system.tables WHERE database={db:String} AND name={name:String}",
            parameters={"db": settings.CLICKHOUSE["database"], "name": TABLE},
        ).first_row
        if engine != "VersionedCollapsingMergeTree":
            raise RuntimeError("Entity facts table has an incompatible engine")
        window = re.search(r"non_replicated_deduplication_window\s*=\s*(\d+)", definition)
        if window is None or int(window.group(1)) < 1:
            raise RuntimeError("Entity facts table must have insertion deduplication enabled")
        expected_types = ["UUID", "UUID", "UUID", "Int16", "Nullable(DateTime64(6, 'UTC'))",
                          "Nullable(DateTime64(6, 'UTC'))", "Bool", "Int64", "Int8", "UInt64"]
        actual = [(row[0], row[1]) for row in client.query(f"DESCRIBE TABLE {table_name()}").result_rows]
        if actual != list(zip(INSERT_COLUMNS, expected_types)):
            raise RuntimeError("Entity facts table has an incompatible schema")
    finally:
        client.close()


def utc_datetime(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_fact(source):
    fact = {key: source[key] for key in FACT_COLUMNS}
    for key in ("msg_id", "entity_id", "room_id"):
        fact[key] = str(UUID(str(fact[key])))
    for key in ("created_at", "stored_date"):
        date = utc_datetime(fact[key])
        fact[key] = date.isoformat(timespec="microseconds") if date else None
    fact["type"] = int(fact["type"])
    fact["count"] = int(fact["count"])
    if fact["count"] < 1:
        raise ValueError("Entity appearance count must be positive")
    if fact["is_reply"] not in (True, False):
        raise ValueError("is_reply must be a boolean")
    fact["is_reply"] = bool(fact["is_reply"])
    return fact


def insert_rows(client, rows, *, token):
    """Replay exactly the saved payload, token and row order after any error."""
    if not rows:
        return
    values = []
    for row in rows:
        fact = {key: row[key] for key in FACT_COLUMNS}
        for key in ("msg_id", "entity_id", "room_id"):
            fact[key] = UUID(fact[key])
        for key in ("created_at", "stored_date"):
            fact[key] = utc_datetime(fact[key])
        fact["type"] = int(fact["type"])
        fact["count"] = int(fact["count"])
        if fact["count"] < 1 or fact["is_reply"] not in (True, False):
            raise ValueError("Invalid entity fact")
        values.append([*(fact[key] for key in FACT_COLUMNS), int(row["sign"]), int(row["version"])])
    client.insert(
        table_name(), values, column_names=INSERT_COLUMNS,
        settings={"async_insert": 0, "insert_deduplicate": 1, "insert_deduplication_token": str(token)},
    )


@contextmanager
def projection_lock(*, using="default"):
    """Serialize fact writers, including replay of pending insertion blocks."""
    connection = connections[using]
    if connection.in_atomic_block:
        raise RuntimeError("ClickHouse projection must run outside a source transaction")
    key = int.from_bytes(hashlib.sha256(table_name().encode()).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [key])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
