# Message sentiment

The sentiment pipeline classifies stored message text with the configured
sentiment service and saves the label, model version and per-message
processing state. It is an independent cursor-driven backfill; it does not
run NER, indexing or embeddings.

## Launch and topology

Manual endpoint: `POST /api/v1/tg/msg/sentiment`

Scheduled pipeline key: `metadata.sentiment`

| Job | Mode | Workflow | Input lifecycle |
| --- | --- | --- | --- |
| Sentiment backfill | `BACKFILL` | `metadata.sentiment.classify_and_store` | A cursor discovers matching messages up to the launch snapshot. |

The Job has one stage and no child. The endpoint returns a Job ID and whether
an equivalent active Job was reused. See [REST API usage](../api_usage.md)
for request filters.

## Source selection and refresh

The source contains messages with non-empty `annotated_text`, a non-null
`stored_date` and `stored_date` no later than the Job's `source_snapshot_at`.
Optional message filters can narrow by room, tags, reply status, publication
date, ingestion date, language and type.

Messages already marked complete in `SentimentState` are skipped by a normal
run. Set `refresh` to select matching messages regardless of their existing
completed state. Failed or incomplete items remain candidates.

The source cursor orders records by (`stored_date`, id). The snapshot prevents
messages ingested after launch from entering the current run; a later run can
include them.

## Classification and persistence

The single stage cleans `annotated_text` for the sentiment request and calls
the configured sentiment service. It persists the sentiment result and
service/model version, then updates `SentimentState` for that message.

Item-level errors are retained in the stage summary and state. A failure for
one message does not invalidate other messages whose results were stored
successfully.

## Capacity and configuration

| Setting | Default | Meaning |
| --- | ---: | --- |
| `SENTIMENT_BATCH_SIZE` | 10000 | Maximum source message IDs in a Batch. |
| `SENTIMENT_SOURCE_READ_SIZE` | 500000 | Maximum source rows read into one dispatcher page. |
| `METADATA_MAX_INFLIGHT` | 1 | Maximum active Batches per metadata Job. |
| `METADATA_MAX_RETRIES` | 3 | Automatic task retries after the first delivery. |
| `QUEUE_SENTIMENT_CONCURRENCY` | 1 | Worker processes configured for the sentiment queue. |

The service URL, model and credentials are configured through the environment
and documented in [environment variables](../enviroments.md).

## Outcomes and recovery

| Situation | Effect |
| --- | --- |
| Sentiment service returns a result | The message result and model version are saved; `SentimentState` becomes complete. |
| Message has already completed | A normal run skips it; refresh forces it into the source. |
| Individual message classification fails | The failure is retained for that item; other results remain stored. |
| Task-level transient failure | The Batch retries within `METADATA_MAX_RETRIES`. |
| Retry budget is exhausted | The failed Batch remains visible for operator retry or discard. |
| Worker is lost | Lease reconciliation recovers eligible work; item state prevents completed results from needing repetition. |

The Job's source cursor accounts for records selected for this execution;
`SentimentState` is the durable per-message record used to decide whether a
future ordinary run needs to classify a message again.

## Progress and rerun behaviour

`SentimentState` is keyed to each message and records whether classification is
complete. A normal run omits completed messages; refresh includes all messages
matching the source filters, even if they already have a stored result. The
source estimate is taken at launch, while the materialized Job input grows as
the cursor creates durable Batches.

An item-level classification failure is captured without erasing successful
results for other messages in the same Batch. A stage-level error uses the
Job retry budget and replays the Batch. The task updates message state as it
stores results, which lets a later ordinary run omit completed messages.
A `refresh` run intentionally opts out of that skip.

The service receives cleaned annotated text, not the raw Telegram payload.
The configured service path is /sentiment/v1/sentiment; deployment-specific
host, model and authentication remain in the
[environment configuration](../enviroments.md).

## Related documentation

* [Pipeline catalogue](pipelines.md) lists this workflow next to embeddings.
* [REST API usage](../api_usage.md) documents the endpoint request.
* [Processing model](../processing-model.md) describes source cursors, leases
  and retryable Jobs.
* [Environment variables](../enviroments.md) lists sentiment service
  configuration.
* Source implementation: app_metadata/services/jobs.py,
  app_metadata/tasks.py and app_metadata/services/sentiments.py.
