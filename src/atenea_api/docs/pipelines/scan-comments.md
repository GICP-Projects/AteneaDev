# Scanning channel comments

`POST /api/v1/tg/msg/scan` selects valid, non-reply parent messages in followed,
valid, public, non-deleted channels with a usable credential. The launcher
records the selected room IDs and message filters in a `telegram.scan.comments`
root Job. The root uses `backfill` for gradual source discovery, and its
`telegram.process_messages` child uses `live` to receive newly recovered
comments through durable handoffs.

Each batch contains explicit `MessageItem` IDs belonging to one credential.
Room IDs are never interpreted as message IDs. Batch inputs remain immutable
through retries; per-parent progress lives in `processor_metadata.parent_progress`.

## Configuration and source boundaries

| Setting | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH` | `300` | Maximum parent IDs in each comments batch. Must be positive. |
| `TELEGRAM_SCAN_MAX_INFLIGHT` | `4` | Maximum active batches in a scan Job; also bounds the source page size. The example environment sets `1`. |
| `TELEGRAM_SCAN_MAX_RETRIES` | `3` | Automatic stage retries before remaining parents become visible failures. |
| `TELEGRAM_SCAN_SOFT_TIME_LIMIT` | `7200` | Soft worker time limit in seconds. |
| `TELEGRAM_SCAN_TIME_LIMIT` | `7800` | Hard worker time limit in seconds. |
| `TELEGRAM_SCAN_LEASE_SECONDS` | `8400` | Stage lease used by recovery and reconciliation. |
| `QUEUE_TELEGRAM_SCAN_COMMENTS_CONCURRENCY` | `1` | Number of Celery processes consuming the comments queue. |

`max_msgs` is the number of Telegram records fetched per parent **in this
execution**, accumulated across partial retries. Service records consume
Telegram's limit but do not produce stored comments. The public API allows
10–5000 records, with a launcher default of 2500. Reaching the limit completes
that parent for this execution; a future execution can continue from its offset.

Dates have separate purposes:

* `created_at` is Telegram publication time. `createdat_min` and
  `createdat_max` select posts by publication date.
* `stored_date` is ingestion time. `stored_since` is an optional ingestion
  lower bound. `source_snapshot_at` fixes the upper ingestion boundary when
  the Job launches. An old post imported after that boundary belongs to a
  later execution, even if its publication date matches the current filters.
* The first source phase orders known ingestion dates by `(stored_date, id)`.
  A second phase pages historical `stored_date=NULL` parents by `id`, so
  omission of `stored_since` does not silently lose those parents. An explicit
  `stored_since` excludes unknown ingestion dates. NULL rows have no ingestion
  timestamp with which to enforce the temporal boundary; their cursor bounds
  the legacy phase, rather than providing a database-wide frozen snapshot.

The dispatcher reads at most `batch_size × max_inflight_batches` parent IDs
per page, then groups them by credential and chunks each group. It commits all
page batches and the cursor together. Repeating dispatch while any page batch
is awaiting publication or processing creates no further input. Grouping a
mixed-credential page can create more batches than active capacity, but the
page's total parent IDs remains bounded. A normal source run closes after both
date phases end; cancellation and partial finalization retain their existing
Job lifecycle behavior.

## Transactions and concurrency

A batch owns one Telethon client and its event loop. Room traversals may be
scheduled concurrently, but a shared lock serializes parent scans that use the
batch's credential. Between normal parent scans, the worker sleeps for
`SLEEP_AFTER_X_REQUESTS` seconds (currently 10) and logs the pause. It skips
the pause after the last parent or when FloodWait/pause stops further requests.
Telethon paginates each parent's `GetReplies` request in pages of up to 100
messages; `max_msgs` is the per-parent total cap, not one request's page size.
Between pages, the scan logs and sleeps 1.0, 1.5, 2.0, 2.5, then 3.0 seconds
for each later page boundary. This progressive delay resets for each parent.
Django transactions run in independent threads with independent connections.
`sync_to_async(..., thread_sensitive=False)` runs the transaction, and an
`async_to_sync` bridge created on the client's loop sends Telegram calls back
to that same loop. A dedicated executor provides eight transaction threads;
thread connections close after each parent operation. Loop cancellation also
sets a thread-visible event checked before persistence and immediately before
commit. Cancellation observed at either checkpoint rolls back the parent unit.

Each parent is acquired with
`select_for_update(skip_locked=True, of=("self",))`. Its offset is read after
that exclusive lock, which remains held until extraction and persistence end.
The joined room is not locked for the duration of the remote request. A parent
already owned by another transaction is terminally skipped for this batch with
reason `parent_locked`, while other parents advance. It consumes no retries and
does not keep the batch pending. This skip does not invalidate the parent: a
future execution can select it again. The owning transaction might later roll
back, so this batch does not guarantee that another scan completes that parent.
Persistence briefly locks the room to check that its credential/access hash
still matches the request. A reassignment during extraction discards that
uncommitted result and leaves the parent pending without overwriting repaired
room access or advancing the parent's offset.

Only persistence briefly locks the batch. It verifies the execution lease,
merges JSON progress from concurrent room threads and commits the following
as one unit:

1. Newly recovered comments and optional query relationships.
2. The parent's offset, scan timestamp and validity.
3. Handoffs to the child processing Job.
4. The parent's completion state, cumulative fetched count and recovered count.

A persistence error rolls back the complete parent unit. Transactions for
earlier parents remain committed. Another scan must acquire the same parent
lock before reading its offset, so it sees that committed advance. A cancelled
or replaced lease cannot save its late result.

## Outcomes and recovery

| Result | Treatment |
| --- | --- |
| Valid response with no new comments | Complete for this execution, keep `is_valid=True`; later executions can discover new comments. |
| `MsgIdInvalidError` / `MsgTooOldError` | Persist `is_valid=False`; terminal skipped parent with its error reason. |
| Parent missing or no longer eligible | Terminal skip with a visible reason. |
| Parent locked by another transaction | Terminal skip with `parent_locked`; continue other parents without retrying the locked parent in this batch. |
| Timeout or transient RPC error | Commit received comments, offset and partial fetched count; retry the remainder. |
| FloodWait | Commit partial progress, persist the credential's `wait_until`, stop new requests and defer the batch until one minute after the credential becomes available. This wait does not consume the retry budget for other transient errors. |
| Telegram rejects the credential/session | Commit any received parent data, mark the credential invalid and stop further requests. Pending parents follow the stage retry and exhaustion policy. |
| Credential reassigned | Keep pending with `credential_changed`; exhaustion makes it a visible failure. A new execution uses the current assignment. |
| Pause | Commit parents already in flight; stop new parents and release pending work for resume. |
| Cancellation or lease replacement | Reject the late parent save and roll back its transaction. |
| Soft timeout or worker loss | Keep committed parent units; roll back the interrupted unit. Redelivery or lease reconciliation resumes outstanding parents. |

The batch completes only when every original parent is completed or skipped.
Retries neither refetch completed parents nor duplicate their handoffs.
Exhausted retries fail only outstanding parents and preserve successes and
skips. Operators can retry failed Jobs through the existing processing controls;
the stored parent progress and original batch IDs remain intact. The generic
reconciler recovers expired leases and missed source/handoff publications.

## Counts and launch response

The response reports `items_count` as selected parent messages and adds
`rooms_count`, `job_id` and `message_job_id`. The launch count is an estimate
stored as `filters.selected_items_count`. Job `total_items` counts input already
materialized into batches, while `processed_items`, `skipped_items` and
`failed_items` measure actual parent outcomes. It can therefore take several
pages for `total_items` to reach the launch estimate.

Stage summaries distinguish `parent_messages`, `parent_messages_scanned` and
`comments_recovered`. Progress also records `fetched_records`, pending parents
and skip/failure reasons. Recovered-comment output counts include only committed
new rows, whereas fetched counts are the cumulative per-parent Telegram limit.
Downstream message counters belong to the LIVE child Job.

## Deploying this input change

This is a breaking input change. Stop manual comments launches and disable
comments schedules before switching versions. Finish or cancel old comments
Jobs using the existing controls and drain their old queued deliveries before
starting the new comments worker. Then restart the dispatcher and workers
together, and enable comments launches again.

The single development database was checked after this input change and has
no comments schedule passing the retired total_msgs argument, so no cleanup
script or schema migration is needed there. If upgrading another old database,
remove that retired argument from the comments task's stored schedule kwargs
before enabling the new launcher. The public serializer, launcher, forms and
worker no longer accept that execution limit.

The message-input version is `comments_messages_v1` and participates in the
source fingerprint. Workers reject unsupported versions or room content types
as visible failures, rather than reinterpreting their input. Existing room scans
continue using the room-input pipeline and its separate batch-size setting.
The fingerprint also includes a hash of the selected rooms' current credential
bindings, so a new launch after access reassignment can create a new Job rather
than reuse failed batches with stale credentials.

The PostgreSQL tests in `app_telegram/tests/test_comments_jobs.py` cover source
pagination, concurrent locks, atomic rollback, partial recovery, accumulated
limits, FloodWait, invalid/empty results, pause/resume and lease rejection.
