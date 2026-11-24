# Atenea API documentation

This directory contains the operational and architectural documentation for
the Atenea API. It covers local and production deployment, configuration,
REST usage, testing, durable processing and recurring Jobs.

Start with the document that matches the task at hand. The deployment guides
explain how to run the platform; the API guide explains how to use it; and the
processing guides explain how long-running work is persisted and recovered.

## Documentation map

### Deployment and configuration

* [Development deployment](dev_deployment.md) explains how to start the local
  Docker services, initialise the supporting components and run Atenea during
  development.
* [Production deployment](prod_deployment.md) describes the production Docker
  Compose stack, prerequisites, certificates, service startup and verification.
* [Environment variables](enviroments.md) is the reference for `.env` files,
  service connections, worker queues, batch sizes, time limits and retry
  settings.
* [Additional configuration](configuration.md) covers configuration that is
  performed through the Django administration interface, including Telegram
  API credentials and related application settings.

### API usage

* [Staff dashboard](dashboard.md) explains the activity overview, frequency and
  z-score charts, filters, date ranges, and asynchronous loading behavior.

* [REST API usage](api_usage.md) presents the main Atenea workflow, from seed
  ingestion and Telegram collection through enrichment, media processing,
  search and retrieval. It also records request conventions and monitoring
  practices.
* [Cursor pagination implementation](pagination.md) explains how to paginate
  model fields and computed query fields, including grouped entity counts.

### Durable processing

The processing layer is deliberately split into two concerns:

* the scheduler owns durable progress, leases, capacity and lifecycle state;
* each application owns source selection, domain logic, stage implementations
  and the meaning of the identifiers stored in a batch.

That boundary allows Telegram collection, NER, indexing, embeddings and
sentiment analysis to use the same execution protocol without making the
generic scheduler aware of their data models.

* [Processing model](processing-model.md) explains `ProcessingJob`,
  `ProcessingBatch` and `ProcessingHandoff`, including their state machines and
  recovery rules.
* [Pipeline catalogue](pipelines/pipelines.md) lists every registered
  pipeline, explains workflow design and links to its operational guide.
* [Telegram room scan](pipelines/telegram-room-scan.md),
  [channel comments scan](pipelines/scan-comments.md),
  [seed population](pipelines/telegram-populate.md),
  [room access recovery](pipelines/telegram-access.md) and
  [message indexing](pipelines/telegram-index-messages.md) describe the
  Telegram pipelines.
* [NER backfill](pipelines/entity-ner-backfill.md),
  [embeddings](pipelines/metadata-embeddings.md) and
  [sentiment](pipelines/metadata-sentiment.md) describe the enrichment
  pipelines.
* Each guide covers its endpoint or schedule key, source selection, Job and
  Batch topology, limits, retries, progress and recovery.
* [Scheduling](scheduling.md) explains periodic triggers, interval, crontab
  and clocked schedules, lifecycle policies, automatic partial finalisation,
  deduplication and operator actions.

### Storage and indexing

PostgreSQL owns the collected records and annotations. Elasticsearch provides
lexical search and message activity statistics; ClickHouse computes entity
rankings; Qdrant serves vector search. The INDEX stage updates the search
documents and analytical facts from the stored PostgreSQL data.

* [Message indexing](pipelines/telegram-index-messages.md) explains what Atenea
  stores in Elasticsearch and ClickHouse, updates, deletions and retry recovery.
* [Dashboard data and performance](dashboard.md#data-stores-and-entity-rankings)
  explains the analytical queries, backend choice and measured resource needs.
* [ClickHouse configuration](enviroments.md#clickhouse-connection-and-query-resources)
  documents connections, query limits and sizing examples.

### Testing

* [Testing strategy](testing.md) describes the automated test scope, the
  PostgreSQL test dependency, local test commands and optional coverage reports.

## Source of truth

The documentation follows the implementation in these modules:

* [`app_scheduler.models`](../app_scheduler/models.py)
* [`app_scheduler.services.jobs`](../app_scheduler/services/jobs.py)
* [`app_scheduler.schedule_policy`](../app_scheduler/schedule_policy.py)
* [`app_scheduler.registry`](../app_scheduler/registry.py)
* the owning application modules under `app_telegram`, `app_entity` and
  `app_metadata`.

When a pipeline is changed, its workflow keys, handoff structure, retry policy
and scheduling contract should be updated in the relevant document at the
same time.
