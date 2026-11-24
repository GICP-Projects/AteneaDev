# REST API usage

This guide presents the main Atenea REST workflow, from seed ingestion to
monitoring, enrichment, attachment processing, statistics, and retrieval.
Swagger remains the exhaustive reference for request parameters and response
schemas:

- Swagger UI: `http://127.0.0.1:8000/swagger`
- OpenAPI schema: `http://127.0.0.1:8000/schema/`

## Request conventions

### Authentication

Protected endpoints require an API key created through the Django admin:

```http
Authorization: ApiKey <API_KEY>
```

The examples below define reusable shell variables:

```bash
HOST="http://127.0.0.1:8000"
API_KEY="<API_KEY>"
AUTH_HEADER="Authorization: ApiKey ${API_KEY}"
```

Authenticated non-staff users can use the message list, count, vector, entity
ranking, and message statistics read endpoints. Message processing actions
remain staff-only. Room and message Elasticsearch search are public; the
frontend form selectors use a separate `Front-API-Key` header.

Search endpoints may allow anonymous access depending on the endpoint and
deployment policy. Supplying the API key also gives authenticated requests the
configured pagination limits.

### Tags and collection traceability

Tags are assigned to seeds and propagated to the rooms created from them.
Message operations filter by the tags of their source rooms, allowing the same
collection selector to be reused throughout the workflow:

```text
seed ingestion -> population -> scanning -> enrichment/indexing
-> attachment processing -> statistics and retrieval
```

Repeat `tag` to select several tags. `tag_match=any` is the default;
`tag_match=all` requires every supplied tag. PostgreSQL tag filters match
complete, case-sensitive seed or room tag values.

### Dates and repeated parameters

Database-backed endpoints use `DD/MM/YYYY`:

```text
createdat_min=01/01/2024&createdat_max=31/12/2024
```

Elasticsearch range filters use ISO dates separated by `__`:

```text
created_at__range=2024-01-01__2024-12-31
```

List parameters are repeated:

```text
tag=collection-a&tag=collection-b&room=room-a&room=room-b
```

### Asynchronous requests

Ingestion and processing endpoints normally return `202 Accepted` with a query
token:

```json
{
  "token": "550e8400-e29b-41d4-a716-446655440000",
  "message": "The tasks have been queued."
}
```

The token identifies the logged request. Telegram media processing additionally
uses it with `/api/v1/tg/msg/media/download/status` to expose scheduler and
worker progress.

### Pagination

The PostgreSQL message lists, `GET /api/v1/tg/msg` and
`GET /api/v1/tg/msg/vector`, use signed cursors and return no total:

```json
{
  "next": "http://127.0.0.1:8000/api/v1/tg/msg?page-size=100&cursor=<signed-cursor>",
  "previous": null,
  "token": "550e8400-e29b-41d4-a716-446655440000",
  "results": []
}
```

Start without `cursor`, then follow `next` until it is `null`; use `previous`
to move backwards. These links preserve filters and `page-size`. Messages default
to 100 items and vectors to 10; both accept at most 100 items per request.
Messages are ordered by ascending `created_at`, then `id`. Unknown dates come
last, ordered by `id`. There is no limit on the number of pages.

Clients must treat cursors as opaque values. Their signatures prevent
modification; they do not encrypt the contents. A cursor is bound to its endpoint,
filters, and ordering. Sending `page`, a modified cursor, or a cursor from a
different selection returns `400 Bad Request` with the usual trace token.

Request a total only when needed, using the same filters with
`GET /api/v1/tg/msg/count` or `GET /api/v1/tg/msg/vector/count`:

```json
{
  "count": 1250,
  "token": "550e8400-e29b-41d4-a716-446655440000"
}
```

The count routes require the same staff permissions as their lists. They count
the complete filtered selection, independently of pagination. List requests
execute no count query. Neither cursors nor counts freeze the data: the total is
calculated when requested, and insertions, deletions, or updates may change the
selection during traversal.

Other database retrieval endpoints retain `page`, `page-size`, and `count` in
their responses. Elasticsearch search endpoints also retain page-number
pagination; their exact page-size behaviour is documented in Swagger.

## Monitoring workflow

### 1. Ingest seeds

Bulk ingestion accepts up to 1,000 Telegram references and applies a
best-effort strategy, returning created and invalid items separately:

```bash
curl -X POST "${HOST}/api/v1/tg/seed/bulk" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '[
    {
      "link": "https://t.me/channel_one",
      "tags": ["research-collection"]
    },
    {
      "link": "https://t.me/channel_two",
      "tags": ["research-collection"]
    }
  ]'
```

### 2. Resolve seeds into rooms

Populate only the selected collection:

```bash
curl -X POST "${HOST}/api/v1/tg/seed/populate" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"]}'
```

Seed population can also be filtered by resource/title match, language,
resource type, and collection date. Telegram username resolution is limited to
200 operations per credential per day; this does not limit scanning messages
from rooms whose Telegram ID and access hash are already known.

Deleting a seed with `DELETE /api/v1/tg/seed/{id}` or
`POST /api/v1/tg/seed/delete/bulk` preserves related rooms, users and collected
messages, including their NER annotations. The related room/user `seed_item`
reference becomes `null`; their tags and other collected data are retained.

### 3. Scan rooms

Retrieve up to 50,000 messages per selected room:

```bash
curl -X POST "${HOST}/api/v1/tg/room/scan" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"], "max_msgs": 50000}'
```

Room scans can be restricted by room name, tag, language, channel/group type,
or last-update interval. The scheduler API at `/api/v1/scheduler/task` can run
the same Celery pipelines recurrently.

If stored access data becomes invalid, recalculate it for the collection:

```bash
curl -X POST "${HOST}/api/v1/tg/room/access" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"]}'
```

### 4. Scan channel comments

Comment extraction is independent from the main room scan:

```bash
curl -X POST "${HOST}/api/v1/tg/msg/scan" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"], "max_msgs": 2500}'
```

Use this selectively because discussion threads can substantially increase
Telegram requests and the number of stored messages.

The response reports selected parent messages in `items_count`, selected
channels in `rooms_count`, and the root/child Job IDs. `max_msgs` applies per
parent across retries. Comments are collected in configurable batches of
parent-message IDs; see [Scanning channel comments](pipelines/scan-comments.md) for
ingestion boundaries, NULL dates and recovery behavior.

## Message enrichment

Previously collected messages can be reprocessed without scanning their rooms
again:

```bash
# Named Entity Recognition
curl -X POST "${HOST}/api/v1/tg/msg/ner" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"]}'

# Elasticsearch indexing
curl -X POST "${HOST}/api/v1/tg/msg/index" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"]}'

# Embedding inference and Qdrant synchronization
curl -X POST "${HOST}/api/v1/tg/msg/embed" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"]}'

# Sentiment analysis
curl -X POST "${HOST}/api/v1/tg/msg/sentiment" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"]}'

# Embedding-based categorization
curl -X POST "${HOST}/api/v1/tg/msg/categorize" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{"tag": ["research-collection"]}'
```

Common message filters include `room`, `tag`, `tag_match`, `lang`, `type`,
`createdat_min`, `createdat_max`, `stored_since`, and `is_reply`. Embedding
requests additionally support `slot`, `instruct`, and `refresh`.

## Attachments and external URLs

### Download Telegram media selectively

The following request downloads PDF attachments of at most 50 MiB:

```bash
curl -X POST "${HOST}/api/v1/tg/msg/media/download" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{
    "tag": ["research-collection"],
    "extensions": ["pdf"],
    "max_file_size_bytes": 52428800
  }'
```

Downloaded objects are stored in the configured S3-compatible provider.
PostgreSQL retains the source message and room, filename, extension, MIME type,
size, SHA-256 hash, risk flags, object key, and processing state. The catalogue
returns temporary presigned URLs rather than requiring public bucket access.

### Telegram media metadata-only collection

`POST /api/v1/tg/msg/media/download` can inspect Telegram media without
downloading or storing the binary object by sending `metadata_only=true`.

The endpoint considers already scanned messages whose media type is photo,
video, audio, document, or GIF. It supports `room`, `tag`, `tag_match`,
`createdat_min`, `createdat_max`, `stored_since`, `lang`, and `is_reply`.

```bash
curl -X POST "${HOST}/api/v1/tg/msg/media/download" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{
    "tag": ["research-collection"],
    "metadata_only": true,
    "createdat_min": "01/01/2024",
    "createdat_max": "31/01/2024"
  }'
```

Metadata-only rows are stored with `status=skipped` and
`reason=metadata_only`. Available filename, extension, MIME type, byte size,
and risk flags are retained, but no S3 object key, SHA-256 hash, or presigned
download URL is created.

### Inspect media processing progress

The media request returns a token:

```bash
curl -G "${HOST}/api/v1/tg/msg/media/download/status" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "token=<REQUEST_TOKEN>"
```

Scheduler fields include `status`, `total`, `messages_scheduled`,
`chunks_scheduled`, `total_chunks`, `schedule_completed`, and
`scheduling_percent`. Worker fields include `processed`, `downloaded`,
`skipped`, `failed`, `chunks_completed`, and `percent`.

### Collect external download URLs

External hosting references are catalogued but not downloaded:

```bash
curl -X POST "${HOST}/api/v1/tg/msg/external-url/collect" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  --data '{
    "tag": ["research-collection"],
    "use_text_fallback": true
  }'
```

Only built-in or configured whitelisted providers are retained. Additional
domains can be configured with `MEDIA_EXTERNAL_URL_WHITELIST_EXTRA`.

### Retrieve the downloadable catalogue

Retrieve Telegram media and external references grouped by room:

```bash
curl -G "${HOST}/api/v1/tg/msg/downloadable" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "tag=research-collection" \
  --data-urlencode "source=all" \
  --data-urlencode "limit=100" \
  --data-urlencode "max_items=100"
```

Useful catalogue filters include:

- `source`: `all`, `bucket`, or `external_url`;
- `provider`: external URL provider;
- `ext`: repeatable Telegram-media extension;
- `status`: processing state;
- the common room, tag, date, language, and reply filters.

### Telegram downloadable media cleanup

`DELETE /api/v1/tg/msg/downloadable` removes matching downloaded objects from
the configured S3-compatible bucket and marks their metadata rows as `deleted`.
It does not delete external URL catalogue entries.

The endpoint only considers media currently marked as `downloaded`. Every
request must include at least one `room` or `tag` scope. Supported filters are:

- `room`: repeatable room unique name;
- `tag`: repeatable room tag;
- `tag_match`: `any` or `all`;
- `createdat_min` and `createdat_max`;
- `ext`: repeatable extension;
- `min_size_bytes`;
- `dry_run`: preview the match without deleting; defaults to `false`;
- `confirm`: required when the match exceeds
  `MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD`.

Always preview a deletion:

```bash
curl -X DELETE \
  -H "${AUTH_HEADER}" \
  "${HOST}/api/v1/tg/msg/downloadable?tag=research-collection&ext=mp4&dry_run=true"
```

Execute the scoped deletion:

```bash
curl -X DELETE \
  -H "${AUTH_HEADER}" \
  "${HOST}/api/v1/tg/msg/downloadable?tag=research-collection&ext=mp4&dry_run=false&confirm=true"
```

## Retrieval and export

### Retrieve stored messages

Database-backed retrieval supports tags, rooms, dates, storage date, and reply
state:

```bash
curl -G "${HOST}/api/v1/tg/msg" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "tag=research-collection" \
  --data-urlencode "createdat_min=01/01/2024" \
  --data-urlencode "page-size=100"
```

Follow the response's `next` link with the same authorization header to export
the complete selection:

```bash
curl "${NEXT_URL}" -H "${AUTH_HEADER}"
```

If the export needs a total, request it separately with the same filters:

```bash
curl -G "${HOST}/api/v1/tg/msg/count" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "tag=research-collection" \
  --data-urlencode "createdat_min=01/01/2024"
```

### Lexical and structured search

Elasticsearch supports free-text search, exact phrases, tags, room names,
entities, dates, media types, replies, and ordering:

```bash
curl -G "${HOST}/api/v1/tg/msg/search" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "q=credential theft" \
  --data-urlencode "tag=research-collection" \
  --data-urlencode "created_at__range=2024-01-01__2024-12-31" \
  --data-urlencode "ordering=-created_at" \
  --data-urlencode "page=1"
```

Use `qem` for exact phrase search. Repeat `qem` to match several phrases; they
are combined with OR by default. Set `qem_operator=and` to require every phrase:

```bash
curl -G "${HOST}/api/v1/tg/msg/search" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "qem=credential theft" \
  --data-urlencode "qem=crypto wallet" \
  --data-urlencode "qem_operator=or"
```

Use `qem_operator=and` in place of `or` when all phrases must match. Multi-value
Elasticsearch filters use `__`, for example
`entity__terms=example.org__example.net` or `room_name__terms=room-a__room-b`.

### Semantic search

```bash
curl -G "${HOST}/api/v1/tg/msg/ai" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "q=credential theft and illicit marketplaces" \
  --data-urlencode "page=1"
```

Semantic search accepts an optional `instruct`; `empty=true` disables the
default instruction when no custom instruction is supplied.

### Inspect message embeddings

```bash
curl -G "${HOST}/api/v1/tg/msg/vector" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "tag=research-collection" \
  --data-urlencode "page-size=10"
```

Follow `next` to continue. To count only messages that have embeddings, use
the same filters on the separate counter:

```bash
curl -G "${HOST}/api/v1/tg/msg/vector/count" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "tag=research-collection"
```

### Aggregate message activity

Group message counts by day, week, month, or year and optionally calculate a
z-score:

```bash
curl -G "${HOST}/api/v1/stats/msg" \
  -H "${AUTH_HEADER}" \
  --data-urlencode "tag=research-collection" \
  --data-urlencode "createdat_min=01/01/2024" \
  --data-urlencode "createdat_max=31/12/2024" \
  --data-urlencode "group_by=week" \
  --data-urlencode "z_score=true"
```

## Errors and troubleshooting

- `400 Bad Request`: inspect the response's `errors` field and the Swagger
  schema for accepted values and date formats.
- `401 Unauthorized` or `403 Forbidden`: verify the `Authorization: ApiKey ...`
  header and staff permissions.
- `404 Not Found` from media progress: the token is invalid, expired, or no
  progress record exists.
- `FloodWait`: the affected Telegram credential is held until Telegram's wait
  period expires.
- Empty results: verify room/tag propagation, date formats, processing status,
  and whether the relevant indexing or embedding stage has completed.
