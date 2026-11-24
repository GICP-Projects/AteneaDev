# Testing strategy

The automated suite focuses on Atenea's core ingestion, collection, and ETL paths:

- Telegram seed validation, bulk ingestion, collection endpoints, Celery
  orchestration, message/reply extraction, and PostgreSQL persistence.
- NER request preparation and entity persistence.
- Sentiment request handling and model-field updates.
- Embeddings preparation, metadata persistence, and vector synchronization.
- Media policy and storage orchestration already covered by the existing suite.
- Frontend form metadata, periodic-task validation, and message statistics.

The default suite uses PostgreSQL as its real infrastructure dependency because the Django models use
PostgreSQL-specific fields and query features. Telegram, Celery workers, Redis,
Elasticsearch, Qdrant, S3, NER, sentiment, and embeddings providers are replaced by
deterministic test doubles. The tests never use `_data` or `_prod_data`.

Telegram collection tests use a small asynchronous fake with the subset of the
Telethon client interface consumed by Atenea. This exercises message conversion,
offset updates, reply relationships, query relationships, and database writes without
using Telegram credentials or making network requests. Endpoint tests exercise DRF
authentication, validation, response codes, and the hand-off to the collection layer;
pipeline tests inspect workload filtering and Celery signatures without starting a
worker.

## Run locally

From `src/atenea_api`, install the locked development dependencies and start the
ephemeral PostgreSQL service:

```bash
uv sync --locked --group dev
docker compose -f docker-compose.test.yml up -d --wait
```

Run the suite:

```bash
uv run pytest
```

The test database schema is reused while the ephemeral container is running. After
adding or changing migrations, force its recreation once with `uv run pytest
--create-db`.

Stop and remove the ephemeral database when finished:

```bash
docker compose -f docker-compose.test.yml down
```

The Compose file uses `tmpfs`, so its PostgreSQL cluster is intentionally discarded.
It does not mount or modify any repository data directory.

## Scope

The default suite is deterministic and requires no internet access or downloaded ML
models. End-to-end tests against live Telegram credentials and real ML/vector services
are deliberately excluded because they are slow, non-deterministic, and require
secrets. Such checks may be performed manually as deployment smoke tests.

## Optional coverage report

Maintainers can generate a local coverage report when investigating untested code:

```bash
uv run pytest --cov --cov-report=term-missing --cov-report=xml
```

The generated coverage files are local development artifacts and are ignored by Git.

## Optional ClickHouse integration tests

Start `docker-compose.test.yml` and run `uv run pytest --clickhouse`. The additional
tests use only the ephemeral `atenea_test` database at 127.0.0.1:28124. Without
that flag, tests marked `clickhouse` are skipped. They cover signed state before
and after merges, metadata changes, deletions, lost acknowledgements, replay of
pending insertion blocks, date precision and cursor ordering. See
[message indexing](pipelines/telegram-index-messages.md) for the production
write and recovery semantics.
