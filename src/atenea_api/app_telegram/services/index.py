"""Index Telegram search documents and project entity counts to ClickHouse.

PostgreSQL messages and annotations provide the source data. Reads are bounded
by the configured source batch size and finish before network writes.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count
from elasticsearch import helpers
from elasticsearch.dsl import connections

from app_base.api import bulk_add_query_relationships_with_pks
from app_entity.enums import from_name
from app_entity.models import AnnotatedEntity
from app_telegram import clickhouse as ch
from app_telegram.documents import MessageDocument, RoomDocument
from app_telegram.models import EntityAnalyticsOutbox, MessageItem, RoomItem
from app_telegram.services.room_reconciliation import room_metadata_write_lock


logger = logging.getLogger(__name__)


# ======================================================
# =====          CLIENT AND BATCH HELPERS          =====
# ======================================================
# =====   Configured client and bounded batches.   =====
# ======================================================


def _client(client=None):
    return client if client is not None else connections.get_connection()


def _source_batch_size(block_size=None):
    """An explicit read size may lower the configured memory bound."""
    configured = int(settings.ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE)
    if block_size is None:
        return configured
    block_size = int(block_size)
    if block_size < 1:
        raise ValueError("block_size must be positive")
    return min(configured, block_size)


def _bulk_chunk_size():
    return int(settings.ELASTICSEARCH_BULK_CHUNK_SIZE)


def _unique_strings(values):
    return list(dict.fromkeys(str(value) for value in values if value is not None))


def _chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


# ======================================================
# =====      SHARED ELASTICSEARCH OPERATIONS       =====
# ======================================================
# =====  Bulk requests, error checks and refresh.  =====
# ======================================================


def _is_absent_delete(result):
    deleted = result.get("delete", {})
    return deleted.get("status") == 404 and deleted.get("result") == "not_found"


def _write_actions(client, actions, *, chunk_size=None, thread_count=1):
    """Write one bounded batch and reject incomplete bulk responses.

    ``thread_count`` selects sequential or parallel bulk requests. An absent
    document is accepted only for a delete action; other errors reach the caller.
    """
    actions = list(actions)
    if not actions:
        return 0
    chunk_size = _bulk_chunk_size() if chunk_size is None else int(chunk_size)
    thread_count = int(thread_count)
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    if thread_count < 1:
        raise ValueError("thread_count must be positive")

    options = {
        "chunk_size": chunk_size,
        "refresh": False,
        "raise_on_error": False,
        "raise_on_exception": True,
    }
    if thread_count == 1:
        success_count, errors = helpers.bulk(client, actions, **options)
        absent_deletes = sum(_is_absent_delete(error) for error in errors)
        failures = len(errors) - absent_deletes
    else:
        success_count = absent_deletes = failures = 0
        # Consume every result so partial failures cannot acknowledge a batch.
        for succeeded, result in helpers.parallel_bulk(
            client, actions, thread_count=thread_count,
            queue_size=thread_count * 2, **options,
        ):
            if succeeded:
                success_count += 1
            elif _is_absent_delete(result):
                absent_deletes += 1
            else:
                failures += 1

    completed = success_count + absent_deletes
    if failures or completed != len(actions):
        raise RuntimeError(
            f"Bulk indexing failed: {completed} of {len(actions)} documents completed"
        )
    return success_count


def _refresh_index(client, index):
    response = client.indices.refresh(index=index)
    if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
        raise RuntimeError(f"Elasticsearch index {index!r} refresh did not complete cleanly")


# ======================================================
# =====         ROOM AND MESSAGE INDEXING          =====
# ======================================================
# ===== Prepare, index and delete Telegram items.  =====
# ======================================================


def _build_model_actions(document, item_ids):
    """Materialize source rows before HTTP and remove missing source documents."""
    items = list(document.get_queryset(and_filter_fields={"pk__in": item_ids}))
    actions = [document._prepare_action(item, action="index") for item in items]
    present = {str(item.pk) for item in items}
    actions.extend(
        {"_op_type": "delete", "_index": document.Index.name, "_id": str(pk)}
        for pk in item_ids if str(pk) not in present
    )
    return actions


def index_message_ids(
    message_ids, *, block_size=None, es_chunk_size=None, thread_count=1,
    rewrite_messages=True, sync_entities=True,
):
    """Index selected messages and their per-message entity counts.

    Parameters
    ----------
    message_ids : iterable
        MessageItem primary keys to process.
    block_size : int, optional
        Source messages per read, capped by ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE.
    es_chunk_size : int, optional
        Documents per bulk request; defaults to ELASTICSEARCH_BULK_CHUNK_SIZE.
    thread_count : int, default=1
        Concurrent Elasticsearch bulk requests for search documents.
    rewrite_messages : bool, default=True
        Rebuild the full search document. False still updates entity counts.
    sync_entities : bool, default=True
        Reconcile per-message entity facts in ClickHouse.

    Returns
    -------
    dict
        Acknowledged search documents and current analytical facts synchronized.
        Unchanged analytical facts are included; these are not insertion counts.

    Raises
    ------
    RuntimeError
        A write or refresh failed. Durable callers retry the source batch.
    """
    ids = _unique_strings(message_ids or [])
    counts = {"search_messages_indexed": 0, "analytics_entity_facts_synchronized": 0}
    if not ids:
        return counts
    client = _client() if rewrite_messages else None
    document = MessageDocument() if rewrite_messages else None
    batch_size = _source_batch_size(block_size)

    for batch_ids in _chunks(ids, batch_size):
        if rewrite_messages:
            room_ids = MessageItem.objects.filter(pk__in=batch_ids).values_list("room_id", flat=True).distinct()
            with room_metadata_write_lock(room_ids):
                actions = _build_model_actions(document, batch_ids)
                _write_actions(
                    client, actions,
                    chunk_size=es_chunk_size, thread_count=thread_count,
                )
            counts["search_messages_indexed"] += sum(
                action.get("_op_type", "index") == "index" for action in actions
            )
        if sync_entities:
            counts["analytics_entity_facts_synchronized"] += reconcile_message_entities(
                batch_ids, block_size=batch_size,
            )
    if rewrite_messages:
        _refresh_index(client, document.Index.name)
    return counts


def index_room_ids(room_ids, *, block_size=None, es_chunk_size=None, thread_count=1):
    """Index RoomDocument rows with bounded reads and checked bulk writes.

    Parameters
    ----------
    room_ids : iterable
        RoomItem primary keys to process.
    block_size : int, optional
        Source rooms per read, capped by ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE.
    es_chunk_size : int, optional
        Documents per request; defaults to ELASTICSEARCH_BULK_CHUNK_SIZE.
    thread_count : int, default=1
        Concurrent bulk requests.
    """
    ids = list(room_ids or [])
    if not ids:
        return
    client = _client()
    document = RoomDocument()
    for batch_ids in _chunks(ids, _source_batch_size(block_size)):
        with room_metadata_write_lock(batch_ids):
            _write_actions(
                client, _build_model_actions(document, batch_ids),
                chunk_size=es_chunk_size, thread_count=thread_count,
            )
    _refresh_index(client, document.Index.name)


def delete_message_ids(message_ids):
    """Delete search documents and cancel their ClickHouse entity facts.

    Parameters
    ----------
    message_ids : iterable
        Deleted MessageItem primary keys. Already absent documents are accepted.
    """
    ids = _unique_strings(message_ids)
    if not ids:
        return
    client = _client()
    for batch in _chunks(ids, _source_batch_size()):
        _write_actions(client, [
            {"_op_type": "delete", "_index": MessageDocument.Index.name, "_id": pk}
            for pk in batch
        ])
        delete_message_entity_facts(batch)
    _refresh_index(client, MessageDocument.Index.name)


# ======================================================
# =====         CLICKHOUSE FACT INDEXING           =====
# ======================================================
# =====  Build, reconcile and cancel entity facts. =====
# ======================================================


def build_message_entity_facts(message_ids, *, using="default"):
    """Build current entity counts from messages and their NER annotations.

    Parameters
    ----------
    message_ids : iterable
        MessageItem primary keys to aggregate.
    using : str, default="default"
        Django database alias for the source messages and annotations.

    Returns
    -------
    list of dict
        One fact per message/entity pair, identified by their UUIDs.
    """
    message_ids = _unique_strings(message_ids)
    if not message_ids:
        return []
    content_type = ContentType.objects.db_manager(using).get_for_model(MessageItem)
    counts = list(
        AnnotatedEntity.objects.using(using)
        .filter(content_type_id=content_type.pk, object_id__in=message_ids)
        .order_by()
        .values("object_id", "entity_id", "entity__type")
        .annotate(count=Count("pk"))
        .order_by("object_id", "entity_id")
    )
    messages = {
        str(message["pk"]): message
        for message in MessageItem.objects.using(using)
        .filter(pk__in=message_ids)
        .values("pk", "room_id", "created_at", "stored_date", "is_reply")
    }
    facts = []
    for row in counts:
        msg_id = str(row["object_id"])
        message = messages.get(msg_id)
        if message is None:
            continue
        entity_id = str(row["entity_id"])
        facts.append({
            "msg_id": msg_id,
            "entity_id": entity_id,
            "room_id": str(message["room_id"]),
            "type": from_name(row["entity__type"]),
            "count": int(row["count"]),
            "created_at": message["created_at"],
            "stored_date": message["stored_date"],
            "is_reply": message["is_reply"],
        })
    facts.sort(key=_entity_fact_key)
    return facts


def _current_entity_facts(client, *, field, ids, after=None, limit=None):
    if field not in {"msg_id", "entity_id"}:
        raise ValueError("Unsupported projection key")
    params = {"ids": [str(value) for value in ids]}
    where = f"{field} IN {{ids:Array(UUID)}}"
    if after is not None:
        where += " AND (msg_id, entity_id) > ({msg:UUID}, {entity:UUID})"
        params.update(msg=str(after[0]), entity=str(after[1]))
    columns = ", ".join((*ch.FACT_COLUMNS, "version"))
    query = (
        f"SELECT {columns}, sum(toInt64(sign)) AS balance FROM {ch.table_name()} "
        f"WHERE {where} GROUP BY {columns} HAVING balance != 0 "
        "ORDER BY msg_id, entity_id, version"
    )
    if limit is not None:
        query += " LIMIT {limit:UInt64}"
        params["limit"] = limit
    result = []
    for row in ch.query_with_ids(client, query, parameters=params).named_results():
        if row["balance"] != 1:
            raise RuntimeError("Invalid signed entity state; refusing to project over corrupted counts")
        result.append({**ch.normalize_fact(row), "version": int(row["version"])})
    return result


def _drain_entity_outbox(client, *, using="default"):
    """Caller holds projection_lock; replay saved blocks before newer writes."""
    objects = EntityAnalyticsOutbox.objects.using(using)
    while (operation := objects.order_by("id").first()) is not None:
        for index in range(operation.next_block, len(operation.blocks)):
            ch.insert_rows(client, operation.blocks[index], token=f"{operation.token}:{index}")
            # An ambiguous HTTP failure leaves both the token and payload intact.
            objects.filter(pk=operation.pk).update(next_block=index + 1)
        operation.delete(using=using)


def _entity_fact_key(row):
    return row["msg_id"], row["entity_id"]


def _write_entity_changes(client, old, desired, *, using="default"):
    previous = {_entity_fact_key(row): row for row in old}
    if len(previous) != len(old):
        raise RuntimeError("More than one active version for a message/entity pair")
    wanted = {}
    for row in desired:
        normalized = ch.normalize_fact(row)
        wanted[_entity_fact_key(normalized)] = normalized
    changed = sorted(
        key for key in previous.keys() | wanted.keys()
        if (ch.normalize_fact(previous[key]) if key in previous else None) != wanted.get(key)
    )
    if not changed:
        return 0
    with transaction.atomic(using=using):
        operation = EntityAnalyticsOutbox.objects.using(using).create()
        rows = []
        for key in changed:
            if key in previous:
                rows.append({**previous[key], "sign": -1})
            if key in wanted:
                rows.append({**wanted[key], "sign": 1, "version": operation.pk})
        # Keep each INSERT in one partition, even when a source batch spans years.
        partitions = {}
        for row in rows:
            created = ch.utc_datetime(row["created_at"])
            partition = created.year * 100 + created.month if created else 197001
            partitions.setdefault(partition, []).append(row)
        # Fixed blocks and order survive changes to future batch settings.
        size = settings.CLICKHOUSE_BATCH_SIZE
        operation.blocks = [
            group[offset:offset + size]
            for _, group in sorted(partitions.items())
            for offset in range(0, len(group), size)
        ]
        operation.save(using=using, update_fields=["blocks"])
    _drain_entity_outbox(client, using=using)
    return len(changed)


def _reconcile_message_entities(message_ids, *, using="default", client=None):
    ids = sorted({str(value) for value in message_ids})
    if not ids:
        return 0
    with ch.projection_lock(using=using), ch.client_session(client) as client:
        _drain_entity_outbox(client, using=using)
        old = _current_entity_facts(client, field="msg_id", ids=ids)
        desired = build_message_entity_facts(ids, using=using)
        _write_entity_changes(client, old, desired, using=using)
        return len(desired)


def reconcile_message_entities(
    message_ids, *, using="default", block_size=None, client=None,
):
    """Return current facts synchronized, including unchanged facts, in bounded batches."""
    ids = _unique_strings(message_ids)
    return sum(
        _reconcile_message_entities(batch, using=using, client=client)
        for batch in _chunks(ids, _source_batch_size(block_size))
    )


def _delete_entity_facts(ids, *, field, using="default", client=None):
    ids = sorted({str(value) for value in ids})
    if not ids:
        return 0
    total = 0
    with ch.projection_lock(using=using), ch.client_session(client) as client:
        _drain_entity_outbox(client, using=using)
        after = None
        while rows := _current_entity_facts(client, field=field, ids=ids, after=after, limit=10000):
            total += _write_entity_changes(client, rows, [], using=using)
            after = _entity_fact_key(rows[-1])
    return total


def delete_message_entity_facts(message_ids, *, using="default", client=None):
    """Cancel current facts, including facts for already-deleted messages."""
    ids = _unique_strings(message_ids)
    for batch in _chunks(ids, _source_batch_size()):
        _delete_entity_facts(batch, field="msg_id", using=using, client=client)
    return len(ids)


def delete_entity_facts(entity_ids, *, using="default", client=None):
    """Cancel all current ClickHouse facts for deleted entity UUIDs."""
    ids = _unique_strings(entity_ids)
    for batch in _chunks(ids, _source_batch_size()):
        _delete_entity_facts(batch, field="entity_id", using=using, client=client)
    return len(ids)


# ======================================================
# =====        TELEGRAM CELERY INDEX TASKS         =====
# ======================================================
# ===== Filter source items and dispatch writes.   =====
# ======================================================


def _join_filter_dicts(groups):
    """Merge list values from preceding tasks without mutating their filters."""
    combined = {"and_filter_fields": {}, "list_filter_fields": {}}
    for group in groups:
        for category, target in combined.items():
            for key, value in group.get(category, {}).items():
                if isinstance(value, list):
                    target.setdefault(key, []).extend(value)
                else:
                    target[key] = value
    return combined


@shared_task(track_started=True, ignore_result=False)
def items_to_index(
    query_filter_by_model, apply_distinct=False, block_size=None,
    es_chunk_size=None, thread_count=10, token=None,
):
    """Index rooms and messages selected by filters from preceding tasks.

    Parameters
    ----------
    query_filter_by_model : dict or list of dict
        Model names (RoomItem, MessageItem) mapped to ``and_filter_fields`` and
        ``list_filter_fields``. An empty filter selects all items of that model.
        Models omitted from the dictionary are skipped.
    apply_distinct : bool, default=False
        Deduplicate source rows when filtering through relations.
    block_size : int, optional
        Primary keys per read, capped by ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE.
    es_chunk_size : int, optional
        Documents per bulk request; defaults to ELASTICSEARCH_BULK_CHUNK_SIZE.
    thread_count : int, default=10
        Concurrent bulk requests passed to the room and message writers.
    token : str, optional
        Query identifier receiving relationships to successfully indexed items.
    """
    filters = query_filter_by_model
    if not isinstance(filters, list):
        filters = [filters]
    batch_size = _source_batch_size(block_size)

    try:
        for model, document, writer in (
            (RoomItem, RoomDocument(), index_room_ids),
            (MessageItem, MessageDocument(), index_message_ids),
        ):
            groups = [group[model.__name__] for group in filters if model.__name__ in group]
            if not groups:
                continue
            source = document.get_queryset(
                **_join_filter_dicts(groups), apply_distinct=apply_distinct,
            )
            last_id = None
            while True:
                batch = source.filter(pk__gt=last_id) if last_id else source
                ids = list(batch.order_by("pk").values_list("pk", flat=True)[:batch_size])
                if not ids:
                    break
                writer(
                    ids, block_size=batch_size, es_chunk_size=es_chunk_size,
                    thread_count=thread_count,
                )
                if token:
                    bulk_add_query_relationships_with_pks(
                        ids, ContentType.objects.get_for_model(model), token,
                    )
                last_id = ids[-1]
    except Exception:
        # This filter-based task reports errors in logs; durable stages call the
        # ID-based writers directly so their exceptions reach the scheduler.
        logger.exception("Elasticsearch room/message indexing failed")
