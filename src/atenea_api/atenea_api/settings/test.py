"""Settings for the isolated automated test suite."""

import os


# base.py reads some required settings while it is imported. Keep deterministic,
# non-secret defaults here so tests never need the development environment file.
TEST_ENV_DEFAULTS = {
    "ACCESS_TOKEN_EXPIRE_SECONDS": "3600",
    "DJANGO_LOG_LEVEL": "WARNING",
    "DJANGO_SECRET_KEY": "atenea-test-only-secret-key",
    "DJANGO_SERVER_TIMEZONE": "UTC",
    "ELASTICSEARCH_HOST": "unused",
    "JWT_SIGNATURE_SECRET": "atenea-test-only-jwt-secret",
    "NER_SERVICE_HOST": "unused",
    "NER_SERVICE_MAX_DATA_BY_REQUEST": "100",
    "NER_SERVICE_PORT": "80",
    "OPENAI_EMBEDDINGS_MAX_DATA_BY_REQUEST": "100",
    "POSTGRES_DB": "postgres",
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PASSWORD": "postgres",
    "POSTGRES_PORT": "25452",
    "POSTGRES_USER": "postgres",
    "REDIS_HOST": "unused",
    "REDIS_PASSWORD": "unused",
    "REDIS_PORT": "6379",
    "SENTIMENT_SERVICE_HOST": "unused",
    "SENTIMENT_SERVICE_MAX_DATA_BY_REQUEST": "100",
    "SENTIMENT_SERVICE_PORT": "80",
}

for key, value in TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(key, value)

from .development import *  # noqa: E402,F403


DATABASES["default"]["TEST"] = {"NAME": "test_atenea"}

# Tests exercise orchestration but replace external work with mocks.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
ELASTICSEARCH_DSL_AUTOSYNC = False
ELASTICSEARCH_DSL_PARALLEL = False

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
