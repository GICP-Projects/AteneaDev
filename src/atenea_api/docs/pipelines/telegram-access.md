# Telegram room access recovery

Room access recovery resolves access hashes for followed rooms that do not
currently have valid Telegram access data. It is a bounded, one-stage Job.
Each room is looked up with a usable Telegram credential and the result is
stored so later room scans can access the room.

## Launch and topology

Manual endpoint: `POST /api/v1/tg/room/access`

Scheduled pipeline key: `telegram.access.rooms`

| Job | Mode | Workflow | Input lifecycle |
| --- | --- | --- | --- |
| Room access recovery | `LIVE` | `telegram.access.rooms.resolve_hashes` | Rooms are selected and assigned to credentials at launch; the input is then closed. |

This workflow has no child Job. Its response includes the selected room count
and Job ID. See [REST API usage](../api_usage.md) for filters and response
structure.

## Eligible rooms and allocation

The source includes rooms with invalid access data that remain followed and
are not marked deleted. Optional filters narrow by room name, tags, language,
channel status and last-update date. Only credentials that are valid and
whose Telegram wait period has expired are usable for the run.

Rooms are spread across the available credentials, up to the configured
rooms-per-Batch limit for each credential. The limit is capped at 200 because
Telegram restricts daily lookups per authorization. If there are more
eligible rooms than available capacity, the extra rooms remain eligible for
a later launch.

## Resolution stage

The task checks that a room still needs repair before sending a lookup. This
avoids spending another Telegram request if another worker or operation
already repaired the room after the Batch was created.

Successful lookups persist the access hash and mark room access valid. A room
Telegram reports as deleted is skipped and recorded accordingly. Rate-limited
work is deferred and the credential's wait time is persisted. The task does
not scan room messages or create message-processing Handoffs.

## Capacity and configuration

| Setting | Default | Meaning |
| --- | ---: | --- |
| `TELEGRAM_ACCESS_ROOMS_PER_BATCH` | 200 | Maximum room lookups assigned per credential; values above 200 are rejected. |
| `TELEGRAM_ACCESS_MAX_INFLIGHT` | 4 | Maximum active Batches in the Job. |
| `TELEGRAM_ACCESS_MAX_RETRIES` | 3 | Automatic retries after the first task delivery. |
| `QUEUE_TELEGRAM_POPULATE_CONCURRENCY` | 1 | Worker processes for the shared populate/access queue. |

Access recovery and seed population use the `telegram-populate-q` worker queue,
but have independent Job capacities and retry policies.

## Outcomes and recovery

| Outcome | Treatment |
| --- | --- |
| Access data resolves | Room access becomes valid and the room is counted as resolved. |
| Room is already repaired | No duplicate lookup is made; the item is treated as already handled. |
| Room is deleted | The room is counted as a deleted/skipped result. |
| Credential is rate limited | Work is deferred until the credential can be used again; unprocessed rooms remain visible. |
| Transient task error | The Batch retries within the configured retry budget. |
| Retry budget is exhausted | Failed rooms remain represented by a failed Batch for operator retry or discard. |

The Batch summary separates resolved, deleted and deferred outcomes. A
successful Job means all input assigned to that execution reached a terminal
item outcome; it does not imply every room lookup succeeded.

## Source limit, progress and operator interpretation

The maximum room count selected by one launch is bounded by the available
credentials and the per-credential lookup limit. For example, with three
usable credentials and a batch limit of 200, at most 600 rooms enter this
execution. More eligible rooms remain invalid and followed, so a later
access-recovery run can select them again. The Job's active Batch count is
still separately limited by `TELEGRAM_ACCESS_MAX_INFLIGHT`.

The launch count describes the assigned rooms, not successful access repairs.
The stage summary distinguishes resolved rooms, rooms reported deleted and
deferred work. A room that another operation repaired before its Batch runs
does not consume an unnecessary lookup; the worker rechecks access validity.

The access Job is independent of later room scans. Completing access recovery
makes a room eligible for a subsequent scan; it does not trigger that scan
automatically. Inspect the Batch errors before using retry-failed or
discard-failed in the scheduler controls.

## Related documentation

* [Pipeline catalogue](pipelines.md) lists the access workflow and its key.
* [REST API usage](../api_usage.md) has the manual launch example.
* [Processing model](../processing-model.md) explains Batch recovery and
  operator actions.
* [Environment variables](../enviroments.md) documents queues and worker
  settings.
* Source implementation: app_telegram/services/jobs.py,
  app_telegram/services/telegram.py and app_telegram/tasks.py.
