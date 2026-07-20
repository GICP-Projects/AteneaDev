# Microservice testing strategy

The NER and sentiment services are independent uv projects. Their default test suites
exercise request validation, API-key authentication, model lifecycle, HTTP endpoints,
and model adapters without downloading or loading real machine-learning models.

spaCy, Transformers, and CUDA boundaries are replaced with deterministic test doubles.
The ordinary suites require no network access, GPU, service credentials, PostgreSQL,
or other Atenea components.

## Run locally

Enter the microservices directory and install the locked development dependencies:

```bash
cd src/atenea_services
uv sync --locked --group dev --project api_ner
uv sync --locked --group dev --project api_sentiment
```

Run each suite in its project directory so pytest discovers the corresponding
configuration:

```bash
uv run --directory api_ner pytest
uv run --directory api_sentiment pytest
```

Both projects require Python 3.11.9 or later within the Python 3.11 release series.
The smoke tests are skipped during these commands.

## Optional model smoke tests

Smoke tests verify only that a locally available configured model loads and returns a
structurally valid prediction. They do not assert particular entities or sentiment
labels because model outputs may change independently of Atenea.

Run the NER smoke test after installing the configured spaCy models:

```bash
RUN_MODEL_SMOKE_TESTS=1 \
  uv run --directory api_ner pytest -m model_smoke
```

Run the sentiment smoke test with the configured model available in the local
Hugging Face cache. Offline mode prevents an accidental download:

```bash
HF_HUB_OFFLINE=1 RUN_MODEL_SMOKE_TESTS=1 \
  uv run --directory api_sentiment pytest -m model_smoke
```

## Optional coverage report

Maintainers can generate local coverage reports when investigating untested code:

```bash
uv run --directory api_ner pytest \
  --cov --cov-report=term-missing --cov-report=xml
uv run --directory api_sentiment pytest \
  --cov --cov-report=term-missing --cov-report=xml
```

The generated coverage files are local development artifacts and are ignored by Git.
