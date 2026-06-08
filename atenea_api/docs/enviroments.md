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
