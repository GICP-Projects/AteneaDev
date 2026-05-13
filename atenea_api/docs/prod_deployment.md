# Production deployment
The steps in this section allow you to start a production environment.

Create a `.env.prod` file with the API keys, host names, etc. An example of the format is included in `.env.example`. Check the **Prepare .env** section or ask other contributors for more information regarding .env files. ***Please don't include quotes in the .env.prod as it will be used in Docker (which doesn't parse .env quotes).*** REF: https://dev.to/tvanantwerp/don-t-quote-environment-variables-in-docker-268h
NOTE: The *_HOST environment variables can be the container name as they are on the same docker network.

**Before proceeding with the production deployment, make sure to check the [Microservices initialization](#microservices-initialization) and [Elasticsearch initialization](#elasticsearch-initialization) sections.**  
These contain essential setup steps and environment variables required to deploy Atenea properly.

## Production instructions
The production environment runs containers:
- Atenea Backend
- PostgreSQL server. Relational DB where the data is stored. 
- Redis server. Cache DB to use it as broker for the queues.
- Celery queues for default tasks, NER, embeddings, and indexing.
- Sentiment tasks are routed to `sentiment-q`; start an additional worker for that queue if sentiment classification is enabled in the deployment.
- Celery beat server. For task scheduling.

Production deployment (**--env-file <path to .env.prod>**) is done by running the following command:
```bash
docker-compose -f production.yaml --env-file ../.env.prod -p atenea-prod up -d
```

Create a superuser to access the admin panel:
```bash
docker exec -it prod-backend-atenea python manage.py createsuperuser
```

During the first startup, after all migrations have been applied, restart prod-beat (it requires the models in the database to function properly):
```bash  
docker restart prod-beat
```

If sentiment classification is enabled and the production compose file does not
define a dedicated `sentiment-q` worker, add one following the same pattern as
the other Celery workers:
```yaml
atenea-queue-sentiment:
  image: gicp/atenea-server:14.04.26
  container_name: prod-sentiment-queue-atenea
  command: ["bash", "-c", "celery -A atenea_api worker -Q sentiment-q -n AT5@%h -l INFO --concurrency=1"]
  depends_on:
    - atenea-redis
    - atenea-backend
  networks:
    - atenea-network
    - atenea_services_net
  env_file:
    - ../.env.prod
```

### Microservices initialization

Deploy any services for Atenea by following [these](../../atenea_services#development-deploy) instructions.

- **API NER**: Once API NER has been deployed it is necessary to configure Atenea. 
  1. Add the same API KEY from API NER .env into the Atenea `.env.*`, `NER_SERVICE_API_KEY` variable. 
  2. Configure `NER_SERVICE_HOST`, `NER_SERVICE_PORT`, `NER_SERVICE_API_KEY`, and `NER_SERVICE_MAX_DATA_BY_REQUEST` in `.env.*`.

- **API Sentiment**: Once API Sentiment has been deployed it is necessary to configure Atenea.
  1. Add the same API KEY from API Sentiment .env into the Atenea `.env.*`, `SENTIMENT_SERVICE_API_KEY` variable.
  2. Configure `SENTIMENT_SERVICE_HOST`, `SENTIMENT_SERVICE_PORT`, and `SENTIMENT_SERVICE_MAX_DATA_BY_REQUEST` in `.env.*`.

- **Embeddings API**: Configure an OpenAI-compatible embeddings endpoint in Atenea.
  1. Set the `.env.*` variables `OPENAI_EMBEDDINGS_BASE_URL`, `OPENAI_EMBEDDINGS_MODEL`, and, if required, `OPENAI_EMBEDDINGS_API_KEY`.
     Leave `OPENAI_EMBEDDINGS_DIMENSIONS` empty unless your provider explicitly supports custom dimensions.
     Example values:
     `https://api.openai.com/v1` for OpenAI, `http://vllm:8000/v1` for vLLM, or `http://ollama:11434/v1` for Ollama.
     The base URL must include the versioned API root, such as `/v1`, not just the host and port.
  2. Go to `atenea_api/settings/base.py`, find the `OPENAI_EMBEDDINGS` variable, and configure any desired parameters.
     Note: sending `OPENAI_EMBEDDINGS_DIMENSIONS` to a backend that does not support custom output sizes may cause request errors.
     `EMBEDDINGS_PENDING_TIMEOUT_SECONDS` controls when a `pending` sync is treated as stale and retried automatically in normal `/msg/embed` runs. With `refresh=true`, all matching items are recalculated.


### Elasticsearch initialization

Once the `ELASTICSEARCH_APIKEY_ID` and `ELASTICSEARCH_APIKEY_API_KEY` variables are set up, enter into the `DEPLOYMENT_UNIQUE_SUFFIX` variable a unique suffix.

This allows to have unique names for both Elasticsearch indexes and Qdrant collections and prevents collisions with others.

Also, ask an administrator for the public key of the Elastic cluster, so that you can communicate with it securely. This `ca.crt` file should be stored in the `es_cluster > certs` folder.

Initialize Elasticsearch indexes for Django on the first run inside `atenea-backend`. [List of commands](https://django-elasticsearch-dsl.readthedocs.io/en/latest/management.html)  
```bash
# Create the indices and their mapping in Elasticsearch
docker exec -it prod-backend-atenea python manage.py search_index --create

# Or, to recreate and repopulate all indices:
docker exec -it prod-backend-atenea python manage.py search_index --rebuild
```
