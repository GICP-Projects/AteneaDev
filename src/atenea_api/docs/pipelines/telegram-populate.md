# Telegram seed population

Seed population resolves stored seed links against Telegram, stores the
corresponding room or user information and indexes rooms for search. One Job
contains both the resolve and index stages, so the same seed Batch advances
from Telegram lookup to `Elasticsearch` indexing.

## Launch and topology

Manual endpoint: `POST /api/v1/tg/seed/populate`

Scheduled pipeline key: `telegram.populate`

| Job | Mode | Workflow | Input lifecycle |
| --- | --- | --- | --- |
| Seed population | `LIVE` | `telegram.populate.seeds.resolve` → `telegram.index.rooms_from_seeds` | A bounded set of seeds is assigned at launch, the input is closed, and the Job runs its Batches. |

This is a single Job with two stages, not a parent and child. A Batch that
succeeds at resolve advances to room indexing; indexing failure retries from
the indexing stage rather than looking up the seed again.

The endpoint returns the selected seed count and Job ID. For request filters
and response examples, see [REST API usage](../api_usage.md).

## Seed selection and credential allocation

The selector can filter by seed link/resource, title, exact tags with any/all
matching, language, seed type and collection date. Only valid seeds that have
not already been populated are eligible.

At launch, the owner considers valid Telegram credentials whose wait period
has expired. It creates at most one bounded seed Batch per usable credential.
The seed limit for this execution is the number of usable credentials
multiplied by `TELEGRAM_POPULATE_SEEDS_PER_BATCH`. Eligible seeds after that limit remain available for a later run. Each Batch
is associated with one credential so the resolver uses the account assigned
to that work.

## Resolution and indexing

The first stage resolves the seed link through Telegram, obtains full entity
information and access data, and creates or updates the corresponding
`RoomItem` or `UserItem`. It also updates the seed's population/validity state.
Seeds that cannot be resolved are marked invalid; a temporary rate limit can
defer a seed so it remains eligible later.

An existing Room is matched by Telegram ID. Before replacing a public username,
populate reconciles its message URLs and records previous username, title and
description values in the Room's read-only `username_history`. Only changed
fields carry previous values; unchanged fields are null. Simultaneous changes
share one entry. The existing full-channel response supplies the description,
so no additional Telegram request is needed. An about-only change also creates
history and synchronization work. A new Room starts with empty history.
The canonical public link uses the returned username rather than an input alias.
Changes to an existing Room's name or indexed metadata, including description
and tags, accumulate in its internal pending field list and renew the pending
synchronization timestamp. The resolve checkpoint
collects affected Room IDs and emits durable Handoffs to one metadata child per
Populate Job, using the final updated Room state. That child batches up to 20
Rooms and indexes their Room documents. It patches Elasticsearch messages only
for username/about/tags changes and Qdrant only for username changes. Title,
language, type, creation date or history changes need only Room indexing, as described in the
[room scan guide](telegram-room-scan.md#optional-room-identity-check). Newly
created Rooms use normal room indexing and do not need a historical sync Job.

The second stage indexes the rooms produced by the same Batch. A successful
resolve stage is durable before indexing runs. If indexing fails, the retry
continues at the index stage and does not repeat the Telegram lookup.

## Capacity and configuration

| Setting | Default | Meaning |
| --- | ---: | --- |
| `TELEGRAM_POPULATE_SEEDS_PER_BATCH` | 200 | Maximum seeds assigned per usable credential in one run. |
| `TELEGRAM_POPULATE_MAX_INFLIGHT` | 4 | Maximum active Batches in the Job. |
| `QUEUE_TELEGRAM_POPULATE_CONCURRENCY` | 1 | Worker processes assigned to the shared populate/access queue. |

The resolver is deliberately configured with zero automatic Celery retries.
A failure is visible on the Job and can be explicitly retried by an operator.
This makes unresolved entities, rate limits and worker interruptions visible
instead of silently treating them as successful.

## Outcomes and recovery

| Outcome | Treatment |
| --- | --- |
| Seed resolves to a Telegram entity | Seed and entity records are persisted; the Batch advances to room indexing. |
| Seed is unresolved or invalid | The seed is recorded as invalid and is not counted as a successful room lookup. |
| Telegram asks the credential to wait | The seed is deferred; the worker records the wait and the seed can be eligible in a later run. |
| Room indexing fails | Only the index stage needs retrying; resolve output is preserved. |
| Worker dies or stage fails | The Batch remains visible as failed/recoverable; use the processing controls to retry or abandon it. |

Seed population counters describe seed work, while the second stage summary
reports room indexing. A zero-retry task failure does not silently complete
the Job; the Job lifecycle makes the failed Batch available for an explicit
operator decision.

## Source limit, progress and operator interpretation

The selected count is bounded by the usable credential set at launch. If four
credentials are available and the configured per-credential size is 200, at
most 800 seeds enter this execution; additional eligible seeds are left for a
later launch. The Job may have more pending Batches than its active capacity,
but dispatch still respects `TELEGRAM_POPULATE_MAX_INFLIGHT`.

The seed resolve stage reports processed, populated, invalid and deferred
outcomes. Room indexing has its own stage summary, so a seed count and an
indexed-room count should not be read as the same measure. A later populate
run will not ordinarily select a seed already marked populated.

If an active equivalent Job is reused, the endpoint does not create another
independent lookup run. After inspecting the Job and its failed Batch, an
operator can explicitly retry failed work or discard it. A retry of an
indexing failure starts at indexing; it does not call Telegram again.

## Related documentation

* [Pipeline catalogue](pipelines.md) lists this workflow beside the other
  scheduled pipelines.
* [REST API usage](../api_usage.md) documents the endpoint request.
* [Processing model](../processing-model.md) explains stages, leases and
  retry controls.
* [Environment variables](../enviroments.md) lists worker and service
  settings.
* Source implementation: app_telegram/services/jobs.py,
  app_telegram/services/telegram.py and app_telegram/tasks.py.
