# Atenea API

Core Django REST Framework project for Atenea. It exposes the API used to ingest,
process, index, search, and analyze Telegram rooms and messages.

Main responsibilities:
- PostgreSQL persistence for structured data.
- Elasticsearch indexing and keyword search.
- Qdrant-backed vector storage and semantic search.
- Celery pipelines for long-running ingestion and processing tasks.
- Integration with local NER and sentiment microservices.
- Integration with an OpenAI-compatible embeddings provider.

## Index
- [REST API usage](./docs/api_usage.md)
- [Environment file explanation](./docs/enviroments.md)
- [Required external models](./models/README.md)
- [Dev deployment](./docs/dev_deployment.md)
- [Production deployment](./docs/prod_deployment.md)
- [First configuration](./docs/configuration.md)
