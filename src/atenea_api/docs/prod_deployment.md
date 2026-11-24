# Production deployment

This guide describes how to configure and start a new Atenea production
environment with Docker Compose.

The stack includes Django, PostgreSQL, Redis, ClickHouse, MinIO, PgAdmin, Celery workers and
Celery Beat. Elasticsearch and the ML services are deployed separately.

## Prerequisites

- Docker Engine with the Compose plugin.
- A reachable Elasticsearch 8 cluster, an API key and its CA certificate.
- API NER deployed according to the
  [microservices guide](../../atenea_services/README.md).
- DNS, firewall and reverse-proxy rules for the public services.

Configure the production hosts and CORS origins with `DJANGO_EXTRA_ALLOWED_HOSTS`
and `DJANGO_EXTRA_CORS_ORIGINS` in `.env.prod` (see below).

## Environment configuration

Create `.env.prod` in the repository root. Do not commit this file and do not
quote its values. See `.env.example` for the complete variable reference.

At minimum, configure:

```env
ATENEA_VERSION=v0.8.0

DJANGO_SETTINGS_MODULE=atenea_api.settings.production
DJANGO_LOG_LEVEL=INFO
DJANGO_SECRET_KEY=<strong-random-secret>
DJANGO_SERVER_TIMEZONE=Europe/Madrid
DJANGO_EXTRA_ALLOWED_HOSTS=<public-host-or-ip>
DJANGO_EXTRA_CORS_ORIGINS=<https://front-end-origin>
JWT_SIGNATURE_SECRET=<different-strong-random-secret>
ACCESS_TOKEN_EXPIRE_SECONDS=86400
REFRESH_TOKEN_EXPIRE_SECONDS=99999999

POSTGRES_HOST=atenea-postgres
POSTGRES_PORT=5432
POSTGRES_DB=postgres
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<strong-random-password>

PGADMIN_DEFAULT_EMAIL=<administrator-email>
PGADMIN_DEFAULT_PASSWORD=<strong-random-password>

REDIS_HOST=atenea-redis
REDIS_PORT=6379
REDIS_PASSWORD=<strong-random-password>

ELASTICSEARCH_HOST=<host-without-scheme-or-port>
ELASTICSEARCH_APIKEY_ID=<api-key-id>
ELASTICSEARCH_APIKEY_API_KEY=<api-key-secret>
DEPLOYMENT_UNIQUE_SUFFIX=<unique-stable-suffix>

CLICKHOUSE_HOST=atenea-clickhouse
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=atenea_prod
CLICKHOUSE_USER=atenea
CLICKHOUSE_PASSWORD=<strong-random-password>

NER_SERVICE_HOST=atenea-services
NER_SERVICE_PORT=80
NER_SERVICE_API_KEY=<same-key-configured-in-api-ner>
NER_SERVICE_MAX_DATA_BY_REQUEST=2000
NER_BATCH_SIZE=10000
NER_EXTRACT_MAX_INFLIGHT=2
NER_BACKFILL_MAX_INFLIGHT=1
NER_HTTP_MAX_CONCURRENCY=2
NER_HTTP_TIMEOUT_SECONDS=300
NER_EXTRACT_SOFT_TIME_LIMIT=900
NER_EXTRACT_TIME_LIMIT=1020
PROCESSING_BATCH_LEASE_SECONDS=1200
PROCESSING_VISIBILITY_TIMEOUT_SECONDS=9000
NER_STORE_MAX_RETRIES=10
NER_RECONCILE_INTERVAL_SECONDS=60

SENTIMENT_SERVICE_HOST=atenea-services
SENTIMENT_SERVICE_PORT=80
SENTIMENT_SERVICE_API_KEY=
SENTIMENT_SERVICE_MAX_DATA_BY_REQUEST=5000

MEDIA_S3_ENDPOINT_URL=http://atenea-minio:9000
MEDIA_S3_PUBLIC_ENDPOINT_URL=http://<production-host>:29000
MEDIA_S3_REGION=us-east-1
MEDIA_S3_BUCKET=atenea-telegram-media
MEDIA_S3_ACCESS_KEY=<strong-random-access-key>
MEDIA_S3_SECRET_KEY=<strong-random-secret-key>
MEDIA_S3_ADDRESSING_STYLE=path
MEDIA_S3_USE_SSL=false
MEDIA_S3_VERIFY_SSL=false
MEDIA_S3_PRESIGNED_TTL_SECONDS=900
MEDIA_S3_MAX_FILE_SIZE_BYTES=52428800
MEDIA_S3_CREATE_BUCKET=true
MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS=86400
MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD=25
```

Generate independent secrets for each component, for example with:

```bash
openssl rand -hex 32
chmod 600 .env.prod
```

Keep `DEPLOYMENT_UNIQUE_SUFFIX` unchanged after initialization because it is
part of the Elasticsearch index and Qdrant collection names.

If embeddings or sentiment analysis are enabled, configure the corresponding
`OPENAI_EMBEDDINGS_*`, `QDRANT_*` and `SENTIMENT_SERVICE_*` variables described
in `.env.example`.

## Elasticsearch certificate

Place the Elasticsearch CA certificate at:

```text
src/atenea_api/es_cluster/certs/ca.crt
```

The certificate is not stored in Git and must be provisioned before building
the backend image. `ELASTICSEARCH_HOST` must not include `https://` or `:9200`.

## NER connectivity

When NER and Atenea run on the same Docker host, they communicate through the
shared `atenea_services_net` network and the `atenea-services` Traefik service.
Use the same `NER_SERVICE_API_KEY` in both deployments.

Verify that the network exists:

```bash
docker network inspect atenea_services_net
```

If NER is hosted elsewhere, configure its reachable host and port instead.

## Deploy

Run production commands from the API directory:

```bash
cd src/atenea_api
```

Validate the configuration:

```bash
docker compose -f docker-compose.yml \
  --env-file ../../.env.prod -p atenea-prod config --quiet
```

The production images are private packages in GitHub Container Registry. On each
deployment host, set `GHCR_USERNAME` to the GitHub account and
`GHCR_READ_PACKAGES_TOKEN` to a classic personal access token with `read:packages`,
then authenticate:

```bash
printf '%s' "$GHCR_READ_PACKAGES_TOKEN" | docker login ghcr.io \
  --username "$GHCR_USERNAME" --password-stdin
```

Set `ATENEA_VERSION` to the same release tag used by the API, NER and sentiment
services. Pull the selected images before starting the stack:

```bash
docker compose -f docker-compose.yml \
  --env-file ../../.env.prod -p atenea-prod pull
```

The API image is shared by Django, Celery workers and Celery Beat. GitHub Actions
builds and publishes it; production hosts only pull it.

Start PostgreSQL and wait until it is ready:

```bash
docker compose -f docker-compose.yml \
  --env-file ../../.env.prod -p atenea-prod \
  up -d atenea-postgres

docker exec prod-postgres-atenea \
  sh -c 'until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do sleep 2; done'
```

Start the complete stack:

```bash
docker compose -f docker-compose.yml \
  --env-file ../../.env.prod -p atenea-prod up -d
```

The backend automatically runs all committed Django migrations before starting
Gunicorn. If a migration fails, the backend exits and the error is shown in its
logs.

## Initial setup

Check the backend and migrations:

```bash
docker compose -f docker-compose.yml \
  --env-file ../../.env.prod -p atenea-prod \
  logs --tail=200 atenea-backend

docker exec prod-backend-atenea python manage.py migrate --check
```

Create the first administrator and restart Celery Beat:

```bash
docker exec -it prod-backend-atenea python manage.py createsuperuser
docker restart prod-beat
```

Create the Elasticsearch indices and mappings:

```bash
docker exec -it prod-backend-atenea python manage.py search_index --create
```

For an imported database containing existing objects, populate the new indices:

```bash
docker exec -it prod-backend-atenea python manage.py search_index --populate
```

Use `search_index --rebuild` only when an intentional full reindex is required;
it deletes and recreates the configured indices.

## Verification

Inspect the containers and run Django's deployment checks:

```bash
docker compose -f docker-compose.yml \
  --env-file ../../.env.prod -p atenea-prod ps

docker exec prod-backend-atenea python manage.py check --deploy
docker exec prod-backend-atenea getent hosts atenea-services
curl http://127.0.0.1:29000/minio/health/live
```

The Compose file publishes:

- Atenea API on host port `28111`.
- PgAdmin on host port `25060`.
- MinIO S3 API on host port `29000`.

`MEDIA_S3_PUBLIC_ENDPOINT_URL` must be reachable by client browsers. For a
public deployment, expose the API and MinIO through a TLS reverse proxy and
restrict direct access to their host ports.

## Persistent data

The bundled services store persistent data under:

```text
src/atenea_api/_prod_data/postgres
src/atenea_api/_prod_data/minio
```

Back up these directories according to the recovery policy of the deployment.
Running `docker compose down` removes containers and networks but preserves
this data. Do not use `down -v` unless named volumes are intentionally being
removed.

## ClickHouse entity analytics

The Compose service `atenea-clickhouse` stores entity ranking facts in
`_prod_data/clickhouse` and is reachable only on the internal network. Configure
`CLICKHOUSE_HOST=atenea-clickhouse`, `CLICKHOUSE_PORT=8123`, an isolated database
and credentials in `.env.prod`. Backend startup applies PostgreSQL migrations
and calls `init_clickhouse` after ClickHouse is healthy.
Back up `_prod_data/clickhouse` together with the PostgreSQL source data.
The bundled server is a single instance with no replication or automatic
failover. Restore its analytical data from backup or rebuild a fresh database
through the existing message indexing workflow using PostgreSQL annotations.
See [message indexing](pipelines/telegram-index-messages.md) for data and retry
semantics and [environment variables](enviroments.md#clickhouse-connection-and-query-resources)
for query resource limits.
