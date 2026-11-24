# Message embeddings

The embeddings pipeline generates vectors from stored message text and
synchronizes them with `Qdrant`. It is a cursor-driven backfill with one
processing stage per message Batch and item-level state for retry and
refresh decisions.

## Launch and topology

Manual endpoint: `POST /api/v1/tg/msg/embed`

Scheduled pipeline key: `metadata.embeddings`

| Job | Mode | Workflow | Input lifecycle |
| --- | --- | --- | --- |
| Embeddings backfill | `BACKFILL` | `metadata.embeddings.generate_and_sync` | A cursor discovers matching messages up to the launch snapshot. |

There is no child Job. The response returns the Job ID and indicates whether
the request created or reused an equivalent active Job. Request examples are
in [REST API usage](../api_usage.md).

## Source selection and refresh

The source is stored messages with `annotated_text` that is not blank,
`stored_date` set and `stored_date` no later than `source_snapshot_at`. Common
message filters narrow by room, tags, reply status, publication date,
ingestion date, language and type.

By default the selector chooses messages whose embedding for the requested
slot is missing, whose processing or `Qdrant` synchronization failed, or whose
pending synchronization has become stale. A message with completed processing
is excluded even if its `Qdrant` row is still pending; this prevents known
non-work, such as text rejected as too short, from being selected forever.
Setting `refresh` includes all otherwise matching messages, including messages
with a completed embedding.

The `slot` option is `default` or `category`. It selects the message's embeddings
or `cat_embeddings` relation, respectively. The optional `instruct` text (up to
128 characters) is passed to the embedding service and stored in Batch
metadata for auditability.

The source cursor is ordered by (`stored_date`, id), and the launch snapshot
excludes later ingested rows. A later run can discover new messages or
revisit failed and stale items.

## Generation and `Qdrant` synchronization

The stage claims each message's embedding state before making external calls.
It sends `annotated_text` to the configured embedding service, writes the
result to the selected embedding slot and synchronizes the vector with
`Qdrant`. Per-message success and failure state is stored, so one failed item
does not erase successful work from the Batch.

The item state is also what makes ordinary later runs selective: missing or
failed work remains eligible, while a completed item is normally skipped.
Pending `Qdrant` synchronization is reconsidered after
`EMBEDDINGS_PENDING_TIMEOUT_SECONDS`, subject to the completed-processing
exclusion described above.

## Capacity and configuration

| Setting | Default | Meaning |
| --- | ---: | --- |
| `EMBEDDINGS_BATCH_SIZE` | 10000 | Maximum message IDs per Batch. |
| `EMBEDDINGS_SOURCE_READ_SIZE` | 100000 | Maximum source rows read into one dispatcher page. |
| `METADATA_MAX_INFLIGHT` | 1 | Maximum active Batches per metadata Job. |
| `METADATA_MAX_RETRIES` | 3 | Stage retries after the first delivery. |
| `EMBEDDINGS_PENDING_TIMEOUT_SECONDS` | 900 seconds | Age after which a still-pending `Qdrant` sync is considered stale. |
| `QUEUE_EMBED_CONCURRENCY` | 1 | Worker processes configured for the embedding queue. |

The external embedding endpoint, model and `Qdrant` connection are configured
through the environment; see [environment variables](../enviroments.md).

## Outcomes and recovery

| Situation | Effect |
| --- | --- |
| Vector generation and synchronization succeed | Message embedding state is marked complete for the selected slot. |
| One message fails | Its error state is retained; other successful messages in the Batch remain successful. |
| `Qdrant` synchronization fails or remains pending too long | The item may be eligible for a later run according to its processing and sync state. |
| Task-level transient failure | The Batch retries within the metadata retry budget. |
| Retry budget is exhausted | The Job exposes failed work for operator retry or discard. |
| refresh is enabled | Matching messages are included even when the selected slot is already complete. |

The source boundary and item states serve different purposes: the snapshot
limits which database rows this run can discover; the embedding state
determines which of those rows need work.

## How item state affects later runs

Eligibility is evaluated for the selected slot, so a completed default
embedding does not by itself make a category embedding complete. Normal
selection is approximately:

| Selected slot state | Included in a normal run? |
| --- | --- |
| No embedding row | Yes |
| Processing failed | Yes |
| `Qdrant` synchronization failed | Yes, unless processing is already marked completed. |
| Synchronization pending beyond the timeout | Yes, unless processing is already marked completed. |
| Processing completed | No |
| Any otherwise matching state with refresh enabled | Yes |

This distinction matters when `Qdrant` and database state disagree. A known
completed result is excluded to avoid endlessly resubmitting it, including a
short-text result whose `Qdrant` row remains pending. A failed processing or
sync state can be selected again. `refresh` is the explicit way to request a
new generation for an already completed slot.

The launch estimate is based on the selector and current state. Later state
changes can affect a future run but do not change the immutable message IDs
already stored in a Batch.

## Related documentation

* [Pipeline catalogue](pipelines.md) lists the metadata processing workflows.
* [REST API usage](../api_usage.md) documents the endpoint parameters.
* [Processing model](../processing-model.md) explains backfill cursors and
  durable stage outcomes.
* [Environment variables](../enviroments.md) documents service and `Qdrant`
  configuration.
* Source implementation: app_metadata/services/jobs.py,
  app_metadata/tasks.py and app_metadata/services/embeddings.py.
