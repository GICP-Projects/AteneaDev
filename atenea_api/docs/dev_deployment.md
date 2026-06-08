# Development deployment
The steps in this section allow you to start your development environment inside your local machine.

Create a `.env.dev` file with the API keys, host names, etc. An example of the format is included in `.env.example`. Check the **Prepare .env** section or ask other contributors for more information regarding .env files. ***Please don't include quotes in the .env.dev as some variables will be used for Docker containers (and Docker doesn't parse .env quotes).*** REF: https://dev.to/tvanantwerp/don-t-quote-environment-variables-in-docker-268h
NOTE: The *_HOST environment variables can be the container name as they are on the same docker network.


## Deployment instructions
The development environment runs these containers:
- PostgreSQL server. Relational DB where the data is stored.
- pgAdmin. PostgreSQL administration UI.
- Redis server. Broker and result backend for Celery queues.
- MinIO server. S3-compatible object storage used for downloaded Telegram media.

In order to run the Redis and Postgresql containers execute the following command inside the atenea_api directory (to use the global .env.dev, available in a different directory, is necessary to specify it by using **--env-file**):
```bash
docker-compose -f development.yaml --env-file ../.env.dev -p atenea-dev up -d
```

The bundled MinIO service exposes:
- S3 API: `http://localhost:29000`
- Web console: `http://localhost:29001`

Use the same values from `.env.dev` for the MinIO console credentials:
`MEDIA_S3_ACCESS_KEY` and `MEDIA_S3_SECRET_KEY`.

## Backend deployment

NOTE: Developed using Python >= 3.11.9 

Create a python3.11 virtual environment, activate it, and install the packages:
```bash
python3.11 -m venv venv
. venv/bin/activate
pip3 install -r requirements.txt
```

----
**NOTE**: `django_elasticsearch_dsl_drf` library is out of date and is not compatible with the latest versions of `elasticsearch_dsl` resulting in an error, to fix this I forked a repo with a patch:
```
>>> requirements.txt
# Fork of django-elasticsearch-dsl-drf with fix for aggs proxy import error (#egg... is required)
git+https://github.com/srfonso/django-elasticsearch-dsl-drf
```
----

Create a development environment (**.env.dev**) by copying the .env.example (remove any # comments) and fill it in.
```bash
cp .env.example .env.dev
vim .env.dev
```

For local media downloads, configure the S3-compatible storage section. If the
Django development server and Celery workers run on the host, use:
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
MEDIA_EXTERNAL_URL_WHITELIST_EXTRA=
```

If Django/Celery also run inside Docker on the same compose network, use the
service name for the internal upload endpoint:
```env
MEDIA_S3_ENDPOINT_URL=http://atenea-minio:9000
MEDIA_S3_PUBLIC_ENDPOINT_URL=http://localhost:29000
```

Local HTTP is the recommended development default. For HTTPS development with a
self-signed certificate, mount MinIO certificates into
`/root/.minio/certs` and set:
```env
MEDIA_S3_ENDPOINT_URL=https://localhost:29000
MEDIA_S3_PUBLIC_ENDPOINT_URL=https://localhost:29000
MEDIA_S3_USE_SSL=true
MEDIA_S3_VERIFY_SSL=false
```

Use `MEDIA_S3_VERIFY_SSL=true` only if the certificate is trusted by the host or
container running Django/Celery.

Run `download.sh` script to add to the project all the required add-ons
```bash
./download.sh 
```

Export the Django settings environment variable, run the necessary migrations, and create a superuser:
```bash
export DJANGO_SETTINGS_MODULE=atenea_api.settings.development

python3 manage.py makemigrations app_*
python3 manage.py migrate
python3 manage.py createsuperuser
```

### Microservices initialization

Deploy any services for Atenea by following [these](../../atenea_services#development-deploy) instructions.

- **API NER**: Once API NER has been deployed it is necessary to configure Atenea. 
  1. Add the same API KEY from API NER .env into the Atenea `.env`, `NER_SERVICE_API_KEY` variable. 
  2. Configure `NER_SERVICE_HOST`, `NER_SERVICE_PORT`, `NER_SERVICE_API_KEY`, and `NER_SERVICE_MAX_DATA_BY_REQUEST` in `.env`.

- **API Sentiment**: Once API Sentiment has been deployed it is necessary to configure Atenea.
  1. Add the same API KEY from API Sentiment .env into the Atenea `.env`, `SENTIMENT_SERVICE_API_KEY` variable.
  2. Configure `SENTIMENT_SERVICE_HOST`, `SENTIMENT_SERVICE_PORT`, and `SENTIMENT_SERVICE_MAX_DATA_BY_REQUEST` in `.env`.

- **Embeddings API**: Configure an OpenAI-compatible embeddings endpoint in Atenea.
  1. Set the `.env` variables `OPENAI_EMBEDDINGS_BASE_URL`, `OPENAI_EMBEDDINGS_MODEL`, and, if required, `OPENAI_EMBEDDINGS_API_KEY`.
     Leave `OPENAI_EMBEDDINGS_DIMENSIONS` empty unless you explicitly need to send it and your backend supports it.
     Example values:
     `https://api.openai.com/v1` for OpenAI, `http://localhost:8000/v1` for vLLM, or `http://localhost:11434/v1` for Ollama.
     The base URL must include the versioned API root, such as `/v1`, not just the host and port.
  2. Go to `atenea_api/settings/base.py`, find the `OPENAI_EMBEDDINGS` variable, and adjust any desired parameters, such as dimensions or concurrency.
     Note: if `OPENAI_EMBEDDINGS_DIMENSIONS` is sent to a backend that does not support custom output sizes, the request may fail. This is common in some vLLM models unless they explicitly support Matryoshka-style dimension reduction.
     `EMBEDDINGS_PENDING_TIMEOUT_SECONDS` controls when a `pending` sync is treated as stale and retried automatically in normal `/msg/embed` runs. With `refresh=true`, all matching items are recalculated.


### Elasticsearch initialization

Once the `ELASTICSEARCH_APIKEY_ID` and `ELASTICSEARCH_APIKEY_API_KEY` variables are set up, enter into the `DEPLOYMENT_UNIQUE_SUFFIX` variable a unique suffix.

This allows to have unique names for both Elasticsearch indexes and Qdrant collections and prevents collisions with others.

Also, ask an administrator for the public key of the Elastic cluster, so that you can communicate with it securely. This `ca.crt` file should be stored in the `es_cluster > certs` folder.

Initialize Elasticsearch indexes for Django on the first run. [List of commands](https://django-elasticsearch-dsl.readthedocs.io/en/latest/management.html)  
```bash
# Create the indices and their mapping in Elasticsearch
python manage.py search_index --create

# or 

# Recreate and repopulate the indices:
python manage.py search_index --rebuild
```

### Run Atenea

Launch the development Django server:
```bash
python manage.py runserver
```



In separated terminal sessions execute different Celery workers that read from each queue (the concurrency can be modified according to requirements):
```bash
export DJANGO_SETTINGS_MODULE=atenea_api.settings.development

# Celery beat
celery -A atenea_api beat -l INFO

# Queue for any task 
celery -A atenea_api worker -Q default -n AT1@%h -l INFO --concurrency=4

# Queue for any task that bulk data into the ES cluster
celery -A atenea_api worker -Q index-q -n AT2@%h -l INFO --concurrency=1

# Queue for any task that uses the NER service
celery -A atenea_api worker -Q ner-q -n AT3@%h -l INFO --concurrency=2

# Queue for tasks that use the configured embeddings provider (changes concurrency
# depending on the computational capacity of that provider)
celery -A atenea_api worker -Q embed-q -n AT4@%h -l INFO --concurrency=1

# Queue for tasks that use the sentiment service
celery -A atenea_api worker -Q sentiment-q -n AT5@%h -l INFO --concurrency=1
```

### Embeddings load balancer

The calculation of embeddings is a resource-intensive process that heavily relies on a very limited resource: the GPU.

Therefore, Atenea offers various ways to control the number of requests sent to the embeddings service:
- Primarily, the task queue, with a default concurrency of 1, allows controlling how many `calculate_embeddings` tasks are executed in parallel.
- Additionally, the `OPENAI_EMBEDDINGS` configuration in `settings.base` enables control over how many texts each `calculate_embeddings` task sends in each request and how many requests it executes concurrently.
