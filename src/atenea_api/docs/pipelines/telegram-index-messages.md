# Telegram message indexing

Message indexing projects eligible PostgreSQL messages and their annotations
into Elasticsearch for lexical search and ClickHouse for entity analytics.
The backfill is a cursor-driven Job: it snapshots the source boundary, reads
bounded pages and retries each immutable message Batch independently.

## Data stores

| Store | Contents and queries |
| --- | --- |
| PostgreSQL | Collected messages, rooms, entities, annotations and processing state; source data for indexing. |
| Elasticsearch | Room and message documents for lexical search, structured filters and message activity statistics. |
| ClickHouse | Message/entity facts for exact frequency and appearance rankings with dates, rooms and other supported filters. |
| Qdrant | Embeddings for semantic search, written by the separate embeddings pipeline. |

### Elasticsearch search documents

`RoomDocument` indexes room descriptions, names, tags and membership metadata.
`MessageDocument` indexes message text, entity names and message metadata,
including room fields, tags, language, dates, views and reply information.
Text fields support lexical and phrase searches; keyword fields support exact
filters. Related room and annotation data are denormalized so search requests
can filter the documents directly. The environment configures the message and
room index names and their optional deployment suffix.

Search still stores the entity names associated with each message. Entity
rankings query ClickHouse's fact table, which contains the per-message counters.
See [lexical search requests](../api_usage.md#lexical-and-structured-search).

### ClickHouse analytical facts

The schema and client are defined in `app_telegram/clickhouse.py`. The
`entity_facts` table represents each annotated `(msg_id, entity_id)` pair:

| Columns | Meaning |
| --- | --- |
| `msg_id`, `entity_id`, `room_id` | PostgreSQL UUIDs; `msg_id` is the message record ID, rather than Telegram's numeric message ID. |
| `type`, `created_at`, `stored_date`, `is_reply` | Entity type and message metadata for filtering; timestamps retain microseconds in UTC. |
| `count` | Number of occurrences of this entity in the message. |
| `sign`, `version` | Signed state and its version for updates, cancellations and exact aggregation. |

The engine is `VersionedCollapsingMergeTree(sign, version)`, ordered by
`(msg_id, entity_id)` and partitioned by creation month. Null creation dates use
the `197001` partition. These partitions preserve precise date filters; rows
are not weekly or monthly summaries.

For each entity, rankings calculate `sum(toInt64(sign))` as message frequency
and `sum(toInt64(sign) * count)` as total appearances. They are exact before
background merges. `COUNT(*)` would count physical rows, including
state and cancellation pairs; dashboard queries use the signed sums without
`FINAL` or `OPTIMIZE`. Entity names and current room/tag selections are resolved
through PostgreSQL. See [ranking pagination](../pagination.md#clickhouse-entity-ranking)
and [backend choice and performance](../dashboard.md#data-stores-and-entity-rankings).

## Launch and topology

Manual endpoint: `POST /api/v1/tg/msg/index`

Scheduled pipeline key: `telegram.index.messages`

| Job | Mode | Workflow | Input lifecycle |
| --- | --- | --- | --- |
| Message index | `BACKFILL` | `telegram.index.messages.write` | A source dispatcher pages eligible messages until the snapshot-bound source is exhausted. |

The response contains the Job ID and selected-item estimate. This is a
single-stage Job with no child. For request filters and response examples, see
[REST API usage](../api_usage.md).

## Source filters and snapshot

The source is stored `MessageItem` records with a non-null `stored_date`. Filters
can select rooms, exact tags, replies, language, type, publication time
(`created_at`) and ingestion time (`stored_since`). These dates are different:
`created_at` is when Telegram published a message; `stored_date` is when Atenea
ingested it.

The Job records a `source_snapshot_at` when it is created. Rows ingested after
that boundary are excluded from this run even if their publication date
matches the filters. Source order and cursor use (`stored_date`, id), making
successive pages stable and unambiguous when timestamps tie.

Equivalent active requests are normally deduplicated using the normalized
processor, mode, workflow and filters. A new request after the active Job
ends can index newly ingested messages.

## Batching and dispatch

| Setting | Default | Meaning |
| --- | ---: | --- |
| `INDEX_MANUAL_BATCH_SIZE` | 100000 | Maximum message IDs placed in a manual index Batch. |
| `INDEX_MAX_INFLIGHT` | 1 | Maximum active Batches for the manual index Job. |
| `INDEX_MAX_RETRIES` | 3 | Automatic stage retries after the first delivery. |
| `QUEUE_INDEX_CONCURRENCY` | 1 | Worker processes for the index queue. |

The cursor dispatcher reads only a bounded number of rows based on Batch size
and available Job capacity. It does not advance to another page while
unpublished Batches from the current page remain pending. Once the query is
exhausted it marks the source exhausted and closes input; already-created
Batches continue through indexing.

## Index stage and failure semantics

The single stage performs a strict bulk update of message documents in the
search index and reconciles their entity counts in ClickHouse. A failure in
either destination is raised to the scheduler. The immutable Batch remains
the retry unit; the two destinations do not share a transaction.

| Situation | Effect |
| --- | --- |
| All documents are written | Batch completes and index counters advance. |
| Transient search-index failure | Batch retries with its original message IDs. |
| Retry budget is exhausted | Batch is FAILED and the Job waits for operator or schedule policy action. |
| Worker dies after accepting a task | Lease expiry recovery republishes the Batch if it remains recoverable. |

The index Job updates search documents and analytical facts from annotations
already stored in PostgreSQL. It does not run NER, embeddings or sentiment; those are separate pipeline keys and Jobs.

The INDEX summary records two destinations: **Elasticsearch** counts acknowledged
message search documents; **ClickHouse** counts current message/entity pairs
synchronized, including unchanged pairs. These figures are recorded after the
stage succeeds, using data already prepared for indexing. They are not counts of
new insertions or total entity appearances. With `reindex_messages=false`, search
is marked as skipped while analytical facts are still synchronized.

Older completed Batches retain their stored summary without a notice or invented
destination counts. A Job mixing old and new summaries marks the available counts
as partial. Viewing the dashboard never reconstructs historical writes or queries
either indexing backend for these counters.

### Updates, deletions and analytical retries

Indexing compares the desired facts with their current signed state. A changed
fact cancels its exact previous row and inserts a new version. Deletion inserts
the cancellation alone; an unchanged fact produces no write. Cancellations
retain the previous dates, room, type and count so filtering remains correct
after metadata changes. Manual/admin deletions enqueue synchronization Jobs.
NER batches project their stored annotation changes through the INDEX stage.

`EntityAnalyticsOutbox` persists pending insertion blocks and stable retry
tokens in PostgreSQL. A PostgreSQL advisory lock serializes projection writers;
each writer drains pending blocks before creating newer changes. ClickHouse
insertion deduplication and the saved tokens let an ambiguous request be
replayed with the same payload. The table engine alone does not deduplicate
task retries. This outbox is pending delivery state, not a full copy of the fact
table. Keep its insertion format and tokens stable while operations are pending.

Search and analytical projections are eventually consistent with PostgreSQL.
Multi-block updates can briefly expose intermediate counts. Existing processing
retries recover failed work; there is no separate daily aggregation task.

## Counts and recovery

The source count captured at launch is an estimate of matching messages;
cursor dispatch materializes those records in bounded pages. The Job's
materialized-item counter grows as pages become durable Batches. Batch stage
summaries record indexing outcomes; search-index counters do not stand in for
the number of messages originally found by the source query.

Operators can inspect the Job and Batch errors, then use retry-failed or
discard-failed from the scheduler controls. Retrying the Job preserves
successful Batches and re-executes only failed input.

## Cursor restart and progress interpretation

The source dispatcher only runs while the backfill Job is `RUNNING` and has
capacity for another Batch. It reads the next ordered page, writes its Batches
and cursor atomically, and waits until that page's queued work is accounted
for before reading further. On source exhaustion it closes input; any
materialized Batches can still be running or waiting for retry.

This means the launch estimate and Job total_items can differ temporarily.
The estimate is computed from the matching source query at launch, whereas
total_items reflects input materialized into durable Batches. A failure in the
search service affects stage outcomes, not which message IDs were in the
source Batch.

The write is safe to retry for the same message IDs. If a Batch fails
permanently, retry-failed acts on that Batch while already-completed Batches
remain complete. A backfill paused during cursor discovery resumes as `RUNNING`
so its source dispatcher can continue. See the
[processing model](../processing-model.md) for the generic resume rule.

## Related documentation

* [Pipeline catalogue](pipelines.md) lists this stage beside the other
  registered workflows.
* [REST API usage](../api_usage.md) shows the endpoint request.
* [Processing model](../processing-model.md) explains source cursors, leases
  and Job lifecycle.
* [Environment variables](../enviroments.md) is the worker configuration
  reference.
* [Dashboard data and performance](../dashboard.md#data-stores-and-entity-rankings)
  explains analytical backend selection and measured query costs.
* Source implementation: app_telegram/services/jobs.py,
  app_telegram/services/pipelines.py, app_telegram/services/index.py,
  app_telegram/tasks.py and app_telegram/clickhouse.py.
