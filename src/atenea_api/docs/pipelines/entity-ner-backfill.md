# Entity NER backfill

The NER backfill finds eligible stored messages, extracts entities and
annotations through the configured NER service, and persists the results.
Each source Batch passes through extraction and storage stages in one Job.
Optional reindexing adds the existing message-index stage to that same
workflow.

## Launch and topology

Manual endpoint: `POST /api/v1/tg/msg/ner`

Scheduled pipeline key: `entity.ner.backfill`

| Job | Mode | Workflow | Input lifecycle |
| --- | --- | --- | --- |
| NER backfill | `BACKFILL` | `entity.ner.extract` → `entity.ner.store` | Cursor-driven source pages stop at the Job snapshot. |
| NER with reindex enabled | `BACKFILL` | `entity.ner.extract` → `entity.ner.store` → `telegram.index.messages.write` | Indexing is an additional stage of each same Batch, not a child Job. |

The endpoint returns a Job ID and whether a new Job was created or an
equivalent active Job was reused. See [REST API usage](../api_usage.md) for
request fields.

## Message eligibility

The source contains messages whose `annotated_text` is non-empty, whose
language is supported by the configured NER service and whose `stored_date` is
not null and no later than `source_snapshot_at`. Optional filters can restrict
rooms, tags, replies, publication date, ingestion date, language and type.
Publication time is `created_at`; `stored_since` filters ingestion time.

By default the source avoids repeating completed work whose input has not
changed. A durable NER state and input hash identify completed extraction.
Incomplete, failed or changed messages can be selected again. Set refresh to
force processing of otherwise completed messages.

The source cursor orders by (`stored_date`, id). It reads bounded pages and
waits for pending Batches before moving the cursor further, so a large source
does not have to be loaded into memory or the broker at once.

## Extraction, storage and indexing

The extraction stage claims eligible message state, calls the configured
NER service, and stores its output as a durable intermediate result. Service
metadata and version are recorded with the Batch so the output can be
audited.

The storage stage writes entities and annotations transactionally and marks
the message state complete. It checks the input hash again: if the message
changed after extraction, stale output is discarded and extraction is
restarted for the new content. The durable claim prevents two active NER
Batches from processing the same message at once.

With reindex enabled, the Batch advances to `telegram.index.messages.write`
after storage. Without it, storage is the final stage. An index failure can
therefore be retried independently from extraction and storage.

## Capacity and configuration

| Setting | Default | Meaning |
| --- | ---: | --- |
| `NER_BATCH_SIZE` | 10000 | Maximum source message IDs in a Batch. |
| `NER_`BACKFILL`_MAX_INFLIGHT` | 1 | Maximum active Batches in a backfill Job. |
| `NER_EXTRACT_MAX_RETRIES` | 3 | Extraction retries after the first delivery. |
| `NER_STORE_MAX_RETRIES` | 10 | Storage retries after the first delivery. |
| `INDEX_MAX_RETRIES` | 3 | Retry budget for the optional index stage. |
| `QUEUE_NER_`BACKFILL`_CONCURRENCY` | 1 | Worker processes for the NER backfill queue. |

Extraction and storage use separate queues and retry budgets. The exact
service endpoints, worker limits and credentials are listed in
[environment variables](../enviroments.md).

## Outcomes and recovery

| Situation | Effect |
| --- | --- |
| Message already has current completed NER state | The source omits it unless refresh is requested. |
| NER extraction succeeds | Intermediate output is saved; the Batch advances to storage. |
| Message content changes before storage | The old intermediate result is rejected and extraction is repeated for the current content. |
| Extraction fails transiently | Only the extraction stage retries. |
| Storage fails transiently | The durable extraction result remains available while storage retries. |
| Retry budget is exhausted | The Batch and Job expose a recoverable failure; completed message states are retained. |
| Optional index stage fails | Retry the index stage without repeating successful NER work. |

The source snapshot defines which messages this run can discover. A later
message arrival or content update may require a later backfill. Job counters
report Batch input and stage summaries; NER state is the per-message source
of truth for whether a message needs future work.

## Per-message state and progress interpretation

The Job's source estimate counts matching candidate messages before
batching. The source dispatcher later materializes bounded pages, so a
partially dispatched backfill can have fewer materialized items than its
estimate. `NERState` is the durable record for an individual message; the Job
and Batch records describe the execution that claimed and processed it.

A completed state is reused only while the normalized input hash still
matches the message. This makes ordinary backfills selective without treating
old output as valid after message text or language changes. refresh deliberately
bypasses the normal completed-state skip for matching messages. A per-message
claim keeps overlapping Jobs from simultaneously storing competing results.

The retry budgets are stage-specific. Extraction retries call the NER
service again; storage retries can reuse the saved intermediate result.
Reindex is another stage on the same Batch, so its failure does not require
re-running the external NER request.

## Related documentation

* [Pipeline catalogue](pipelines.md) shows the registered NER key and its
  place in composed Telegram processing.
* [REST API usage](../api_usage.md) documents the NER request.
* [Processing model](../processing-model.md) explains multi-stage Batches,
  source cursors and recovery.
* [Environment variables](../enviroments.md) lists NER service and worker
  settings.
* Source implementation: app_entity/services/jobs.py,
  app_entity/tasks.py and app_telegram/services/pipelines.py.
