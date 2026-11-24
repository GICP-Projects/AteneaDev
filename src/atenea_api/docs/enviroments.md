# Prepare .env
Create a `.env.dev` by creating a copy of the `.env.example` file and fill in its contents.

## Environment Variables

### `DJANGO_SETTINGS_MODULE` **Required**
Default: none.

Set it to `atenea_api.settings.development` for a development deployment or
`atenea_api.settings.production` for a production deployment.

Select the configuration file to be used by the Django server. All settings that
are common in both production and development are located in
`atenea_api.settings.base`.

### `DJANGO_SECRET_KEY` **Required**
Default: none.

In Django, the `SECRET_KEY` setting is a vital security feature. It's a random, secret string used for cryptographic signing, ensuring data integrity. It's crucial for signing session cookies and preventing Cross-Site Request Forgery (CSRF) attacks.

To create a valid security key run the following command and copy the result into the environment variable.
```python
python manage.py shell -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### `DJANGO_SERVER_TIMEZONE` **Required**
Default: `Europe/Madrid`.

- Dictates how Django stores datetime information in databases without timezone support.
- Provides timezone-aware datetime objects if you're using the USE_TZ setting set to `True`.
- Determines the timezone for datetime calculations if not using timezone-aware datetime objects.

### `DJANGO_EXTRA_ALLOWED_HOSTS` **Optional**
Default: empty.

Comma-separated hostnames or IPs appended to Django's `ALLOWED_HOSTS`
(`localhost` and `127.0.0.1` are always allowed). Set it to the domain or IP the
API is reached through, e.g. `api.example.org,10.0.0.5`. Requests with any other
`Host` header are rejected with `400 Bad Request`.

### `DJANGO_EXTRA_CORS_ORIGINS` **Optional**
Default: empty.

Comma-separated front-end origins (scheme, host and port) appended to the CORS
whitelist, e.g. `http://10.0.0.5:3001`. `http://localhost:3001` is always
allowed (and `http://127.0.0.1:3001` in production).

### `REDIS_PASSWORD` **Required**
Default: none.

Redis password to be used by the docker. This variable will be used by the docker-compose container `redis` and by the API server to enable communication between the two.

### Telegram media object storage: `MEDIA_S3_*` **Optional / Required for media download**
Defaults: local MinIO-compatible values.

Atenea can download Telegram message media from already scanned `MessageItem`
rows and store the binary files in an S3-compatible object store. PostgreSQL
stores only metadata, state, hash, risk flags, bucket name, and object key.

The backend supports MinIO, AWS S3, Cloudflare R2, Wasabi, DigitalOcean Spaces,
Backblaze B2 S3-compatible buckets, and other S3-compatible providers.

- `MEDIA_S3_ENDPOINT_URL`: internal endpoint used by Django/Celery to upload and
  sign objects. In local development with the provided compose file and a backend
  running on the host, use `http://localhost:29000`. If Django/Celery run inside
  Docker, use `http://atenea-minio:9000`.
- `MEDIA_S3_PUBLIC_ENDPOINT_URL`: public-facing endpoint for humans/browsers.
  Keep it aligned with the endpoint users can reach, for example
  `http://localhost:29000` in local development or `https://media.example.org`
  in production.
- `MEDIA_S3_REGION`: S3 region. MinIO accepts values such as `us-east-1`.
- `MEDIA_S3_BUCKET`: bucket where Telegram media objects are stored.
- `MEDIA_S3_ACCESS_KEY` and `MEDIA_S3_SECRET_KEY`: credentials used by Atenea and
  by the MinIO container when the bundled MinIO service is used.
- `MEDIA_S3_ADDRESSING_STYLE`: use `path` for MinIO and many S3-compatible
  services. Use `virtual` only when your provider requires virtual-hosted style.
- `MEDIA_S3_USE_SSL`: set to `true` when `MEDIA_S3_ENDPOINT_URL` uses HTTPS.
- `MEDIA_S3_VERIFY_SSL`: controls TLS verification. Use `true` for public CA
  certificates, `false` only for local/self-signed development, or an absolute
  path to a CA bundle inside the Django/Celery container, for example
  `/etc/ssl/certs/minio-ca.crt`.
- `MEDIA_S3_PRESIGNED_TTL_SECONDS`: expiration time for signed download URLs.
- `MEDIA_S3_MAX_FILE_SIZE_BYTES`: default maximum file size, in bytes, accepted
  by the media download endpoint. The request can override it up to the
  serializer limit.
- `MEDIA_S3_CREATE_BUCKET`: if `true`, Atenea attempts to create the bucket when
  uploading the first object.
- `MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS`: time in seconds that Redis keeps media
  download progress counters after the request token is created. Default: `86400`
  seconds.
- `MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD`: number of bucket media rows that
  can be deleted with `dry_run=false` before the request must also include
  `confirm=true`. Default: `25`.

Typical local development values when Django/Celery run on the host:
```env
MEDIA_S3_ENDPOINT_URL=http://localhost:29000
MEDIA_S3_PUBLIC_ENDPOINT_URL=http://localhost:29000
MEDIA_S3_REGION=us-east-1
MEDIA_S3_BUCKET=atenea-telegram-media
MEDIA_S3_ACCESS_KEY=<random-access-key>
MEDIA_S3_SECRET_KEY=<random-secret-key>
MEDIA_S3_ADDRESSING_STYLE=path
MEDIA_S3_USE_SSL=false
MEDIA_S3_VERIFY_SSL=false
MEDIA_S3_PRESIGNED_TTL_SECONDS=900
MEDIA_S3_MAX_FILE_SIZE_BYTES=52428800
MEDIA_S3_CREATE_BUCKET=true
MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS=86400
MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD=25
```

### `MEDIA_EXTERNAL_URL_WHITELIST_EXTRA` **Optional**
Default: empty.

Comma-separated list of extra external download/cloud domains to include in the
downloadable catalog. Atenea ships with a built-in whitelist for common providers
such as Google Drive, Dropbox, OneDrive, Mega, MediaFire, WeTransfer, Box,
iCloud, pCloud, GitHub raw files, GitLab, Archive.org, IPFS gateways, and
Nextcloud/OwnCloud patterns.

Example:
```env
MEDIA_EXTERNAL_URL_WHITELIST_EXTRA=files.example.org,downloads.example.net,nextcloud.my-org.org
```

### `DEPLOYMENT_UNIQUE_SUFFIX` **Optional**
Default: empty string.

To avoid collisions between different deployments sharing the same Elasticsearch
and/or Qdrant cluster.

The final names follow the environment naming pattern plus the optional suffix:
- Elasticsearch in dev with `DEPLOYMENT_UNIQUE_SUFFIX`="": `dev_msg` and `dev_room`
- Elasticsearch in prod with `DEPLOYMENT_UNIQUE_SUFFIX`="001": `prod_msg_001` and `prod_room_001`
- Qdrant in dev with `DEPLOYMENT_UNIQUE_SUFFIX`="": `dev_msg_search_embeddings` and `dev_categorization_embeddings`
- Qdrant in prod with `DEPLOYMENT_UNIQUE_SUFFIX`="001": `prod_msg_search_embeddings_001` and `prod_categorization_embeddings_001`

### `ELASTICSEARCH_APIKEY_ID` and `ELASTICSEARCH_APIKEY_API_KEY` **Required**
Default: none.

The ES cluster credentials to allow access to the Elasticsearch API.

To create a new API Key for this project, go to the Kibana webpage and log in with an account with permissions, go to `Stack Management > API Keys > Create API Key` copy the key in JSON format, and extract from it the ID and the api_key. 

### ClickHouse connection and query resources

ClickHouse stores the analytical facts produced by Atenea's INDEX stage.
Configure its HTTP connection separately from Elasticsearch:

| Variable | Default / use |
| --- | --- |
| `CLICKHOUSE_IMAGE` | `clickhouse/clickhouse-server:26.8`, the bundled server image. |
| `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT` | Host defaults: `127.0.0.1:28123`; inside Compose use `atenea-clickhouse:8123`. |
| `CLICKHOUSE_DATABASE` | Explicit deployment database; otherwise development/production settings derive a name with the deployment suffix. |
| `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD` | User defaults to `atenea`; configure a password for the bundled service. |
| `CLICKHOUSE_SECURE`, `CLICKHOUSE_VERIFY` | HTTPS disabled by default; TLS certificate verification enabled. |
| `CLICKHOUSE_CONNECT_TIMEOUT`, `CLICKHOUSE_REQUEST_TIMEOUT` | Connection/read timeout: `10` / `180` seconds. |
| `CLICKHOUSE_BATCH_SIZE` | `50000` rows per analytical insertion block. |
| `CLICKHOUSE_DEDUPLICATION_WINDOW` | `10000` insertion blocks retained for retry deduplication. |
| `CLICKHOUSE_MAX_THREADS` | `8` query threads as a general starting limit. |
| `CLICKHOUSE_MAX_MEMORY_BYTES` | `8589934592`: 8 GiB maximum tracked memory per query. |
| `CLICKHOUSE_EXTERNAL_GROUP_BY_BYTES` | `2147483648`: spill aggregation state to disk after 2 GiB. |

The current development host uses the following settings for a 444.3M-fact
table, with 36 physical CPU cores and 376 GiB RAM:

```dotenv
CLICKHOUSE_MAX_THREADS=16
CLICKHOUSE_MAX_MEMORY_BYTES=34359738368
CLICKHOUSE_EXTERNAL_GROUP_BY_BYTES=17179869184
```

These are limits per query: 16 threads, 32 GiB maximum tracked memory and a
16 GiB spill threshold. Memory is allocated as needed. Size the limits for
concurrent requests and other services; leave memory above the spill threshold
for merging aggregation states. `.env.example` retains the smaller general
defaults. Restart Django and Celery to pick up changes; clients send query
settings without restarting ClickHouse or rebuilding data. See the
[measured comparison](dashboard.md#data-stores-and-entity-rankings).

### `GOOGLE_OAUTH2_CLIENT` and `GOOGLE_OAUTH2_SECRET` **Optional**
Default: none.

Client ID and Client Secret used to enable Google OAuth2 authentication through
`social_django` and `drf_social_oauth2`.

These variables are required only when Google OAuth2 login or token conversion
is enabled.

- `GOOGLE_OAUTH2_CLIENT`: It's a public identifier for your application.
- `GOOGLE_OAUTH2_SECRET`: This is a secret known only to the application and the authorization server. It is used to prove the identity of the application to Google's authorization server.

### `JWT_SIGNATURE_SECRET` **Required when OAuth2/JWT tokens are enabled**
Default: none.

Secret used to sign the JWT-formatted access and refresh tokens generated by
the custom token generator configured in `OAUTH2_PROVIDER`.

It can be generated using the same command as `DJANGO_SECRET_KEY`. It is kept separate from the Django secret so JWT signing can be configured independently from Django's own cryptographic signing.

> Note: Atenea generates signed JWT-formatted tokens, while request
> authentication is handled by Django OAuth Toolkit / DRF OAuth2 authentication.
> The backend does not currently decode JWT payloads as the source of request
> authentication decisions.

### `ACCESS_TOKEN_EXPIRE_SECONDS` and `REFRESH_TOKEN_EXPIRE_SECONDS`
Default: `86400` and `99999999`.

`ACCESS_TOKEN_EXPIRE_SECONDS` configures the lifetime of OAuth2 access tokens
before they must be regenerated.

`REFRESH_TOKEN_EXPIRE_SECONDS` is kept in the environment template for future
configuration symmetry, but it is not currently applied by `OAUTH2_PROVIDER`.
Refresh token rotation is enabled through Django OAuth Toolkit.

### `NER_SERVICE_API_KEY` and `OPENAI_EMBEDDINGS_API_KEY` **Required / Optional**
Defaults: `NER_SERVICE_API_KEY` has no default. `OPENAI_EMBEDDINGS_API_KEY` defaults to empty.

The API KEYs to allow access to the microservices APIs.
`OPENAI_EMBEDDINGS_API_KEY` can be left empty when using an OpenAI-compatible
server that does not require authentication.

### NER service connection

`NER_SERVICE_HOST` and `NER_SERVICE_PORT` identify the API-NER service from the
Django/Celery network. They have no application fallback and must be configured.
`NER_SERVICE_API_KEY` must match the key configured by API-NER when its
authentication is enabled.

`NER_SERVICE_MAX_DATA_BY_REQUEST` limits the number of texts sent in one HTTP
request to API-NER. The application fallback is `2000`. This is distinct from
`NER_BATCH_SIZE`: one durable batch may contain 10,000 messages and be sent to
API-NER as several requests of at most 2,000 texts each.

`NER_METADATA_TIMEOUT_SECONDS` sets the timeout, in seconds, for fetching
API-NER metadata before processing a batch. Default: `20`.

### Sentiment service connection

`SENTIMENT_SERVICE_HOST` and `SENTIMENT_SERVICE_PORT` identify the sentiment
service from the Django/Celery network; both are required. Set
`SENTIMENT_SERVICE_API_KEY` to the service key if authentication is enabled;
otherwise leave it empty. `SENTIMENT_SERVICE_MAX_DATA_BY_REQUEST` is required
and limits the number of messages sent in each sentiment HTTP request. The
example uses `5000`.

### Durable ProcessingJob and pipeline tuning

These settings tune the durable database-backed jobs and their Celery workers.
Values below are application fallbacks; an explicit `.env` value takes
precedence. A *batch size* is the number of durable input IDs in one batch,
whereas a service's `MAX_DATA_BY_REQUEST` controls how many items go in one
HTTP call.

#### NER batch sizing and capacity

| Variable | Default | Meaning |
| --- | ---: | --- |
| `NER_BATCH_SIZE` | `10000` | Message IDs per NER batch. |
| `NER_EXTRACT_MAX_INFLIGHT` | `2` | Maximum in-flight batches for a live NER Job. |
| `NER_BACKFILL_MAX_INFLIGHT` | `1` | Maximum in-flight batches for a manual NER backfill Job. |
| `NER_HTTP_MAX_CONCURRENCY` | `2` | Number of shared Redis semaphore slots for concurrent batch calls to API-NER across workers using that Redis broker. |
| `NER_HTTP_TIMEOUT_SECONDS` | `300` | HTTP timeout for NER extraction requests. |
| `NER_METADATA_TIMEOUT_SECONDS` | `20` | HTTP timeout for each API-NER metadata lookup. |

NER implementation versions are constants next to their code in `app_entity`,
not deployment environment variables. Increment the custom-entity version
when its extraction rules change, and the input-normalizer version when the
canonical NER input changes. The normalizer version participates in the input
hash, so changing it makes matching prior results eligible for a later NER
pass. Other version changes are recorded for audit; a corpus-wide rerun remains
an explicit `refresh` operation. API-NER reports its own service, model, and
output-schema versions through `/metadata`; Atenea records that metadata for
each batch instead of maintaining a duplicate schema-version setting.

#### NER task limits and recovery

| Variable | Default | Meaning |
| --- | ---: | --- |
| `NER_EXTRACT_SOFT_TIME_LIMIT` / `NER_EXTRACT_TIME_LIMIT` | `900` / `1020` seconds | Soft and hard Celery time limits for extraction. |
| `NER_EXTRACT_LEASE_SECONDS` | `1200` seconds | Lease for the NER extraction stage and its Redis semaphore slot. |
| `NER_EXTRACT_MAX_RETRIES` | `3` | Celery retries for transient NER extraction failures. |
| `NER_STORE_SOFT_TIME_LIMIT` / `NER_STORE_TIME_LIMIT` | `900` / `1020` seconds | Soft and hard Celery time limits for storing NER results. |
| `NER_STORE_LEASE_SECONDS` | `1200` seconds | Lease for the NER storage stage. |
| `NER_STORE_MAX_RETRIES` | `10` | Retries for storing an extracted result; a storage retry reuses the persisted NER result. |
| `PROCESSING_BATCH_LEASE_SECONDS` | `1200` seconds | Generic fallback lease; a stage-specific lease overrides it. |
| `PROCESSING_VISIBILITY_TIMEOUT_SECONDS` | `9000` seconds | Redis visibility timeout before an unacknowledged Celery message can be redelivered. Keep it longer than the longest hard task limit. |
| `NER_RECONCILE_INTERVAL_SECONDS` | `60` seconds | Beat interval for reconciling expired leases and work that needs republishing. |

#### Celery queue worker concurrency

These variables control the worker concurrency configured for each Celery queue. Development deployment reads them from `.env.dev`; production Compose reads them from `.env.prod`. They configure worker capacity and do not change the `max_inflight_batches` value already stored on an existing `ProcessingJob`.

| Variable | `.env.example` | Meaning |
| --- | ---: | --- |
| `QUEUE_DEFAULT_CONCURRENCY` | `4` | Worker concurrency for the default queue. |
| `QUEUE_INDEX_CONCURRENCY` | `1` | Worker concurrency for indexing. |
| `QUEUE_TELEGRAM_SCAN_CONCURRENCY` | `1` | Worker concurrency for Telegram room scans. |
| `QUEUE_TELEGRAM_SCAN_COMMENTS_CONCURRENCY` | `1` | Worker concurrency for Telegram comment scans. |
| `QUEUE_TELEGRAM_POPULATE_CONCURRENCY` | `1` | Worker concurrency for Telegram population. |
| `QUEUE_NER_LIVE_CONCURRENCY` | `1` | Worker concurrency for live NER extraction. |
| `QUEUE_NER_BACKFILL_CONCURRENCY` | `1` | Worker concurrency for NER backfill extraction. |
| `QUEUE_NER_STORE_CONCURRENCY` | `1` | Worker concurrency for NER storage. |
| `QUEUE_EMBED_CONCURRENCY` | `1` | Worker concurrency for embedding tasks. |
| `QUEUE_SENTIMENT_CONCURRENCY` | `1` | Worker concurrency for sentiment tasks. |

#### Other durable pipeline sizes and capacity

| Variable | Default | Meaning |
| --- | ---: | --- |
| `INDEX_MANUAL_BATCH_SIZE` | `100000` | Message IDs per manual Index batch. |
| `INDEX_MAX_INFLIGHT` | `1` | In-flight batches per manual Index Job. |
| `INDEX_MAX_RETRIES` | `3` | Celery retries for transient Index task failures. |
| `INDEX_SOFT_TIME_LIMIT` / `INDEX_TIME_LIMIT` / `INDEX_LEASE_SECONDS` | `3600` / `3900` / `4200` seconds | Index task limits and stage lease. |
| `TELEGRAM_SCAN_ROOMS_PER_BATCH` | `300` | Room IDs per normal Scan batch, grouped by Telegram credential. |
| `TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH` | `300` | Parent-message IDs per Scan Comments batch, grouped by credential; independent parent transactions retain partial retry progress. |
| `TELEGRAM_SCAN_MAX_INFLIGHT` | `4` | In-flight scan batches per Job. |
| `TELEGRAM_ROOM_METADATA_ROOMS_PER_BATCH` | `20` | Maximum Rooms per metadata synchronization Batch. |
| `TELEGRAM_ROOM_METADATA_MAX_INFLIGHT` | `2` | In-flight Room metadata Batches per synchronization Job. |
| `TELEGRAM_SCAN_MAX_RETRIES` | `3` | Retries for transient scan task failures. |
| `TELEGRAM_SCAN_SOFT_TIME_LIMIT` / `TELEGRAM_SCAN_TIME_LIMIT` / `TELEGRAM_SCAN_LEASE_SECONDS` | `7200` / `7800` / `8400` seconds | Scan task limits and stage lease. |
| `TELEGRAM_POPULATE_SEEDS_PER_BATCH` | `200` | Seeds per Populate batch. |
| `TELEGRAM_POPULATE_MAX_INFLIGHT` | `4` | In-flight Populate batches per Job. |
| `TELEGRAM_POPULATE_SOFT_TIME_LIMIT` / `TELEGRAM_POPULATE_TIME_LIMIT` / `TELEGRAM_POPULATE_LEASE_SECONDS` | `3600` / `3900` / `4200` seconds | Populate task limits and stage lease. Populate does not automatically retry a partially resolved batch; unresolved seeds remain eligible for a later run. |
| `TELEGRAM_ACCESS_ROOMS_PER_BATCH` | `200` | Rooms per access-hash recovery batch. The code rejects values above Telegram's 200-lookup-per-auth daily limit. |
| `TELEGRAM_ACCESS_MAX_INFLIGHT` | `4` | In-flight access batches per Job. |
| `TELEGRAM_ACCESS_MAX_RETRIES` | `3` | Retries for transient access recovery failures. |
| `TELEGRAM_ACCESS_SOFT_TIME_LIMIT` / `TELEGRAM_ACCESS_TIME_LIMIT` / `TELEGRAM_ACCESS_LEASE_SECONDS` | `3600` / `3900` / `4200` seconds | Access task limits and stage lease. |

#### Embeddings and Sentiment batch processing

| Variable | Default | Meaning |
| --- | ---: | --- |
| `EMBEDDINGS_SOURCE_READ_SIZE` | `100000` | Maximum source page read by the embeddings backfill dispatcher before it materializes durable batches. |
| `EMBEDDINGS_BATCH_SIZE` | `10000` | Message IDs per embeddings batch. |
| `SENTIMENT_SOURCE_READ_SIZE` | `500000` | Maximum source page read by the sentiment backfill dispatcher. |
| `SENTIMENT_BATCH_SIZE` | `10000` | Message IDs per sentiment batch. |
| `METADATA_MAX_INFLIGHT` | `1` | In-flight batches per embeddings or sentiment Job. |
| `METADATA_MAX_RETRIES` | `3` | Celery retries for transient metadata task failures. |
| `METADATA_SOFT_TIME_LIMIT` / `METADATA_TIME_LIMIT` / `METADATA_LEASE_SECONDS` | `3600` / `3900` / `4200` seconds | Embeddings and sentiment task limits and stage lease. |

`NER_LIVE_REGISTRATION_CHUNK_SIZE` is not a current setting and has been
removed from the examples; do not rely on it to tune scans.

### `OPENAI_EMBEDDINGS_BASE_URL` **Optional**
Default: `https://api.openai.com/v1`.

Base URL of the OpenAI-compatible embeddings API.
Typical examples are `https://api.openai.com/v1`, `http://localhost:8000/v1`
for vLLM, and `http://localhost:11434/v1` for Ollama.
It must point to the versioned API root, not just the host and port.
For example, use `http://localhost:28333/v1` instead of `http://localhost:28333`.

### `OPENAI_EMBEDDINGS_MODEL` **Optional**
Default: `text-embedding-3-large`.

It must match the exact model name exposed by that backend.
For example, OpenAI uses names such as `text-embedding-3-large`, while local
servers often expose repository-style names like `Qwen/Qwen3-Embedding-4B`.

### `OPENAI_EMBEDDINGS_MAX_DATA_BY_REQUEST` **Optional**
Default: `80`.

Maximum number of texts sent in a single request to the embeddings provider.

### `OPENAI_EMBEDDINGS_MAX_PARALLEL_REQUESTS` **Optional**
Default: `12`.

Maximum number of embeddings requests sent in parallel from one Atenea task.

### `OPENAI_EMBEDDINGS_TIMEOUT` **Optional**
Default: `60`.

HTTP timeout in seconds for the embeddings client.

### `OPENAI_EMBEDDINGS_DIMENSIONS` **Optional**
Default: empty / provider native dimension.

Leave it empty to let the embeddings provider return its native dimension.
This is the safest option for OpenAI-compatible servers such as vLLM or Ollama,
because some models reject the `dimensions` request parameter unless they
explicitly support custom output sizes.

### `EMBEDDINGS_PENDING_TIMEOUT_SECONDS` **Optional**
Default: `900`.

Defines when a `pending` embeddings sync is
considered stale and retried automatically by `/msg/embed` without `refresh=true`.
With `refresh=true`, Atenea forces recalculation for all matching items anyway.

### `QDRANT_URL` **Optional**
Default: `http://localhost:6333`.

Base URL of the Qdrant server.

### `QDRANT_API_KEY` **Optional**
Default: empty.

API key used to authenticate against Qdrant when the server requires it.

### `QDRANT_TIMEOUT` **Optional**
Default: `60`.

HTTP timeout in seconds for Qdrant requests.

### `QDRANT_COLLECTION_MESSAGE_SEARCH` and `QDRANT_COLLECTION_CATEGORIZATION` **Optional**
Defaults: `msg_search_embeddings` and `categorization_embeddings`.

Base collection names for message-search and categorization vectors. The
development and production settings add their respective `dev_` / `prod_`
prefixes and the optional `DEPLOYMENT_UNIQUE_SUFFIX`.

### `QDRANT_SEARCH_HNSW_EF` **Optional**
Default: empty (Qdrant default).

Query-time `ef` for HNSW search.
Higher values usually improve recall/precision but increase latency.
Good starting values for high-dimensional vectors are `256` or `512`.

### `QDRANT_SEARCH_EXACT` **Optional**
Default: empty (Qdrant default ANN search).

If set to `true`, Atenea will request exact kNN search in Qdrant.
This is useful for quality benchmarking, but it is significantly slower and
not recommended for production traffic.

### `QDRANT_HNSW_M` **Optional**
Default: empty (Qdrant collection default).

HNSW graph connectivity (`m`) for new collections created by Atenea.
Higher values usually improve recall and RAM/index build cost.

### `QDRANT_HNSW_EF_CONSTRUCT` **Optional**
Default: empty (Qdrant collection default).

HNSW build-time candidate depth (`ef_construct`) for new collections created by
Atenea. Higher values typically improve index quality at the cost of slower
index construction.

### `QDRANT_HNSW_FULL_SCAN_THRESHOLD` **Optional**
Default: empty (Qdrant collection default).

HNSW planner threshold (`full_scan_threshold`) for new collections created by
Atenea.

> Note: `QDRANT_HNSW_*` values are applied only when Atenea creates a collection.
> They do not modify existing collections automatically.

### Reindex HNSW in an existing collection (without recalculating embeddings)

If you change `m` and/or `ef_construct` in an existing Qdrant collection, you do
not need to recalculate embeddings. Qdrant rebuilds the HNSW index using the
vectors already stored in the collection.

Update collection HNSW config:
```bash
curl -X PATCH "http://<qdrant-host>:6333/collections/<collection_name>" \
  -H "Content-Type: application/json" \
  -H "api-key: <api-key>" \
  --data-raw '{
    "hnsw_config": {
      "m": 32,
      "ef_construct": 200
    }
  }'
```

Watch optimization/indexing status:
```bash
watch -n 2 "curl -s -H 'api-key: <api-key>' http://<qdrant-host>:6333/collections/<collection_name> | jq '.result.status, .result.optimizer_status'"
```

If this update looks almost instant, that is normal: the API call only applies
the config change request. Internal optimization can be very fast when there is
little pending work, or when your current index is already close to the target
configuration.

### `QDRANT_UPSERT_BATCH_SIZE` **Optional**
Default: `1000`.

Maximum number of vector points sent to Qdrant in a single upsert request.
Useful to avoid timeouts when a Celery block processes many items at once.
If this value is too large for your Qdrant node/network, upserts may fail with
timeout errors. In that case, reduce it (for example, `400` worked well in a
real deployment with large batches).

### `QDRANT_UPSERT_MAX_RETRIES` **Optional**
Default: `2`.

Number of retries for each Qdrant upsert batch before the client falls back to
splitting that batch into smaller chunks.

### `QDRANT_UPSERT_RETRY_DELAY_SECONDS` **Optional**
Default: `2`.

Base delay in seconds used between Qdrant upsert retries.
