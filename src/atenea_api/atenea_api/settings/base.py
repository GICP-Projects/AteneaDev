import os
import logging
from pathlib import Path
import tomllib
from django.utils import timezone
from atenea_api.settings.services import (
    BaseServiceConfig,
    NERServiceConfig,
    OpenAIEmbeddingsConfig,
    QdrantConfig,
    ServiceAuthConfig,
    ServiceEndpointConfig,
)
from app_entity.job_keys import (
    NER_EXTRACT_STAGE,
    NER_PROCESSOR,
    NER_STORE_STAGE,
)
from app_metadata.job_keys import (
    EMBEDDINGS_PROCESSOR,
    EMBEDDINGS_STAGE,
    SENTIMENT_PROCESSOR,
    SENTIMENT_STAGE,
)
from app_telegram.job_keys import (
    TELEGRAM_ACCESS_STAGE,
    TELEGRAM_INDEX_MESSAGES_PROCESSOR,
    TELEGRAM_INDEX_ROOMS_STAGE,
    TELEGRAM_INDEX_STAGE,
    TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE,
    TELEGRAM_ROOM_METADATA_STAGE,
    TELEGRAM_POPULATE_STAGE,
    TELEGRAM_SCAN_COMMENTS_PROCESSOR,
    TELEGRAM_SCAN_COMMENTS_STAGE,
    TELEGRAM_SCAN_STAGE,
)


def env_list(name):
    """Return the non-empty, comma-separated values of an environment variable."""
    return [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]


# ======================================================
# =====            PLATFORM SETTINGS               =====
# ======================================================

# Avoiding throttling in bulk_create and bulk_update 
BULK_BATCH_SIZE = 2500

# DATE FORMAT & MIN DATE
FORMAT_DATE = "%d/%m/%Y"
MIN_DATETIME = timezone.datetime.strptime("01/01/2000", FORMAT_DATE)
MIN_DATE = MIN_DATETIME.date()

# INTERNATIONALIZATION
# https://docs.djangoproject.com/en/4.1/topics/i18n/
LANGUAGE_CODE = "en"
LANGUAGES = (
    ("en", "English"),
    ("es", "Spanish"),
)

# PLATFORM TIMEZONE
# https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
TIME_ZONE = os.getenv("DJANGO_SERVER_TIMEZONE")
USE_I18N = True
USE_TZ = True



# ======================================================
# =====       DJANGO APPS + THIRD PARTIES          =====
# ======================================================

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

PROJECT_APP = [
    "app_base",
    "app_entity",
    "app_metadata",
    "app_telegram",
    "app_frontend",
    "app_scheduler",
    "app_stats",
    "app_users",
    "app_dashboard",
]

THIRD_PARTY_APPS = [
    # Extra fields
    "phonenumber_field",
    # DRF
    "rest_framework",
    "rest_framework_api_key",
    # Celery beat
    "django_celery_beat",
    # For Bearer tokens
    'rest_framework.authtoken',
    "drf_spectacular",
    "oauth2_provider",
    "social_django",
    "drf_social_oauth2",
    "corsheaders",
    # Django Elasticsearch integration
    "django_elasticsearch_dsl",
    # Django REST framework Elasticsearch integration (this package)
    'django_elasticsearch_dsl_drf',
]
INSTALLED_APPS = DJANGO_APPS + PROJECT_APP + THIRD_PARTY_APPS



# ======================================================
# =====        DJANGO FRAMEWORK SETTINGS           =====
# ======================================================

# BASE PATH TO BUILD PATH INSIDE THE PROJECT
BASE_DIR = Path(__file__).resolve().parent.parent.parent

with (BASE_DIR / "pyproject.toml").open("rb") as pyproject_file:
    PROJECT_VERSION = os.getenv("ATENEA_VERSION", "").removeprefix("v") or tomllib.load(
        pyproject_file
    )["project"]["version"]

# 'models' directory 
MODELS_DIR = os.path.join(BASE_DIR, 'models')

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'atenea_api.urls'

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                'social_django.context_processors.backends',
                'social_django.context_processors.login_redirect',
            ],
        },
    },
]

WSGI_APPLICATION = 'atenea_api.wsgi.application'

# PASSWORD VALIDATORS
# https://docs.djangoproject.com/en/4.1/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.1/howto/static-files/
STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/4.1/ref/settings/#default-auto-field
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'



# ===================================================================
# =====             DJANGO REST FRAMEWORK SETTINGS              =====          
# =====    (Default permissions, auth allowed for endpoints)    =====
# ===================================================================
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": (
        # All endpoints require Authentication by default
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_AUTHENTICATION_CLASSES": (
        # AUTHENTICATION METHODS FOR LOGIN USING A PROVIDER
        "oauth2_provider.contrib.rest_framework.OAuth2Authentication",  
        "drf_social_oauth2.authentication.SocialAuthentication",
        "atenea_api.authentication.ApiKeyAuthentication",
    ),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "staff_api": "24000/hour",
        "anon": "300/day",
    },
    'DEFAULT_PAGINATION_CLASS': 'app_base.views.BasePaginator',
    'PAGE_SIZE': 10,
    "UNICODE_JSON": False,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Custom setting:
    'MAX_PAGES_ANON': 100,
    'MAX_PAGES_AUTHENTICATED': 1000,
}



# ======================================================
# =====      DJANGO AUTH GENERAL SETTINGS          =====
# ======================================================
AUTH_USER_MODEL = "app_users.User" # DRF USER MODEL to use

AUTHENTICATION_BACKENDS = (
    'social_core.backends.google.GoogleOAuth2', # Google OAuth2
    'drf_social_oauth2.backends.DjangoOAuth2',  # Default Oauth2 
    'django.contrib.auth.backends.ModelBackend',# Default login (f.e admin site)
)

# Custom drf-social-oauth2 settings
DRFSO2_PROPRIETARY_BACKEND_NAME = 'internal-backend-auth' # Name of the auth method for backend auth
DRFSO2_URL_NAMESPACE = 'drf_social_o2'

# JWT Activated
ACTIVATE_JWT = True

# Custom oauth2 config
OAUTH2_PROVIDER = {
    'ACCESS_TOKEN_EXPIRE_SECONDS': int(os.getenv("ACCESS_TOKEN_EXPIRE_SECONDS")),
    'ROTATE_REFRESH_TOKEN': True,
    #DOESN'T WORKS oauth2_settings.DEFAULTS['REFRESH_TOKEN_EXPIRE_SECONDS'] = int(os.getenv("REFRESH_TOKEN_EXPIRE_SECONDS"))
    
    # Custom JWT tokens
    'ACCESS_TOKEN_GENERATOR': 'app_users.apps.generate_token',
    'REFRESH_TOKEN_GENERATOR': 'app_users.apps.generate_token',
}

# Secret to sign the JWT tokens. 
# NOTE: DIFERENT FROM DJANGO_SECRET BECAUSE ITS GOING TO BE USED IN THE FRONTEND ALSO
JWT_SIGNATURE_SECRET = os.getenv("JWT_SIGNATURE_SECRET")

# ** USE BY THE `rest_framework_api_key` THIRD PARTY APP ** 
API_KEY_CUSTOM_HEADER = "HTTP_FRONT_API_KEY" # Custom header: 'FRONT-API-KEY' 



# ======================================================
# =====            SOCIAL AUTH SETTINGS            =====
# ======================================================
SOCIAL_AUTH_USER_FIELDS = ["email", "username", "first_name", "last_name", "password"]

### GOOGLE ###
SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = os.getenv("GOOGLE_OAUTH2_CLIENT")
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = os.getenv("GOOGLE_OAUTH2_SECRET")

# Define SOCIAL_AUTH_GOOGLE_OAUTH2_SCOPE to get extra permissions from Google.
SOCIAL_AUTH_GOOGLE_OAUTH2_SCOPE = [
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
]



# ===================================================================
# =====                DRF-SPECTACULAR CONFIG                   =====          
# =====                  (API doc webpage)                      =====
# ===================================================================
SPECTACULAR_SETTINGS = {
    "TITLE": "Atenea API",
    "DESCRIPTION": """
    Welcome to the documentation page for our project's API, which aims to provide 
    the necessary tools to offer a search engine for Telegram channels and messages, 
    as well as to provide fully functional ETL pipelines for data ingestion to 
    properly feed the searcher in a stable, parallelizable, and fully scalable manner.
    """,
    "VERSION": PROJECT_VERSION,
    "TOS": "https://gicp.es/",
    #"CONTACT": "tresca.msw@gmail.com",
    "LICENSE": "GICP licence",
    # Described tags for the swagger documentation
    'TAGS': [
        {'name': 'oauth', 'description': 'OAuth2 related authentication and token management'},
    ],
    # In case of having endpoints without `@extend_schema` tags, they will be grouped 
    # by their resource name /api/v1/<resource>
    'SCHEMA_PATH_PREFIX': r'/api/v[0-9]',
    # OTHER SETTINGS
    'OAUTH2_FLOWS': [
        'password',
    ],
    #'OAUTH2_AUTHORIZATION_URL': 'https://accounts.google.com/o/oauth2/v2/auth',
    'OAUTH2_TOKEN_URL': 'http://localhost:8000/auth/token/',  # Cambia la URL según tu configuración
}


# ======================================================
# =====            GLOBAL DATABASE CONF              =====
# ======================================================
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "postgres"),
        "USER": os.getenv("POSTGRES_USER", "postgres"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "postgres"),
        # Container name of the service (production) or localhost (development)
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),  
        # Container default port (5432) (production) or custom external port (development)
        "PORT": os.getenv("POSTGRES_PORT", 5432), 
    }
}


# ======================================================
# =====           BASE CELERY SETTINGS             =====
# ======================================================
CELERY_BROKER_URL = f"redis://:{os.getenv('REDIS_PASSWORD')}@{os.getenv('REDIS_HOST')}:{os.getenv('REDIS_PORT')}/0"
CELERY_RESULT_BACKEND = f"redis://:{os.getenv('REDIS_PASSWORD')}@{os.getenv('REDIS_HOST')}:{os.getenv('REDIS_PORT')}/0"

CELERY_IGNORE_RESULT = True
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    "app_metadata.services.embeddings.calculate_embeddings": {"queue": "embed-q"},
    "app_metadata.services.sentiments.classify_sentiment": {"queue": "sentiment-q"},
    "app_telegram.services.index.items_to_index": {"queue": "index-q"},
    "app_metadata.services.embeddings.items_to_index": {"queue": "index-q"},
    # Generic Job/Batch coordination stays in the scheduler. Application work
    # is routed from its owning app, never from app_scheduler.tasks.
    "app_scheduler.tasks.dispatch_processing_job": {"queue": "default"},
    "app_scheduler.tasks.dispatch_processing_handoffs": {"queue": "default"},
    "app_scheduler.tasks.reconcile_processing_jobs": {"queue": "default"},
    "app_entity.tasks.dispatch_ner_backfill_batches": {"queue": "default"},
    "app_entity.tasks.extract_ner_batch": {"queue": "ner-backfill-q"},
    "app_entity.tasks.store_ner_batch": {"queue": "ner-store-q"},
    "app_telegram.tasks.sync_message_entity_index_batch": {"queue": "index-q"},
    "app_telegram.tasks.scan_rooms_processing_batch": {"queue": "telegram-scan-q"},
    "app_telegram.tasks.scan_comments_processing_batch": {"queue": "telegram-scan-comments-q"},
    "app_telegram.tasks.populate_processing_batch": {"queue": "telegram-populate-q"},
    "app_telegram.tasks.index_messages_processing_batch": {"queue": "index-q"},
    "app_telegram.tasks.index_rooms_from_seeds_processing_batch": {"queue": "index-q"},
    "app_telegram.tasks.dispatch_index_backfill_batches": {"queue": "default"},
    "app_telegram.tasks.dispatch_scan_comments_source_batches": {"queue": "default"},
    "app_metadata.tasks.dispatch_embeddings_backfill_batches": {"queue": "default"},
    "app_metadata.tasks.dispatch_sentiment_backfill_batches": {"queue": "default"},
    "app_metadata.tasks.embed_processing_batch": {"queue": "embed-q"},
    "app_metadata.tasks.sentiment_processing_batch": {"queue": "sentiment-q"},
}

CELERY_ACCEPT_CONTENT = ["application/json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = os.getenv("DJANGO_SERVER_TIMEZONE")
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# New durable batch tasks use late acknowledgements.  Keep the Redis delivery
# lease above their hard limit so a slow NER call is never duplicated merely
# because Redis made the message visible again.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": int(os.getenv("PROCESSING_VISIBILITY_TIMEOUT_SECONDS", "9000")),
}
# To customize your own logging handlers (prevent celery from replacing whatever 
# you have configured for Django with its own loggers)
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler' # Default


# ======================================================
# =====         ELASTICSEARCH SETTINGS             =====
# ======================================================
ELASTICSEARCH_DSL = {
    "default": {
        "hosts": f"https://{os.getenv('ELASTICSEARCH_HOST')}:9200",
        # ssl certs generally are associated with a full domain eg: example.com or www.example.com
        "ssl_assert_hostname": False, # only if using machine IP!
        "verify_certs": True,
        "ca_certs": os.path.join(BASE_DIR, "es_cluster/certs/ca.crt"),
        "api_key": (
            os.getenv("ELASTICSEARCH_APIKEY_ID", None),
            os.getenv("ELASTICSEARCH_APIKEY_API_KEY", None),
        ),
        # ssl connections with Elasticsearch are more prone to timing out,
        # default is 10 seconds without any retries
        "timeout": 60,
        "retry_on_timeout": True,
        "max_retries": 10,
    },
}

ELASTICSEARCH_DSL_AUTOSYNC = True
ELASTICSEARCH_DSL_PARALLEL = True
ELASTICSEARCH_DSL_SIGNAL_PROCESSOR = "app_base.signals.OnlyDeleteSignalES"

# Bound the message IDs read into memory and the actions sent per Elasticsearch request.
ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE = int(os.getenv("ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE", "2500"))
ELASTICSEARCH_BULK_CHUNK_SIZE = int(os.getenv("ELASTICSEARCH_BULK_CHUNK_SIZE", "2500"))

# ClickHouse stores the mutable per-message entity facts used by rankings.
CLICKHOUSE = {
    "host": os.getenv("CLICKHOUSE_HOST", "127.0.0.1"),
    "port": int(os.getenv("CLICKHOUSE_PORT", "28123")),
    "database": os.getenv("CLICKHOUSE_DATABASE") or "atenea",
    "username": os.getenv("CLICKHOUSE_USER", "atenea"),
    "password": os.getenv("CLICKHOUSE_PASSWORD", ""),
    "secure": os.getenv("CLICKHOUSE_SECURE", "false").lower() in {"1", "true", "yes"},
    "verify": os.getenv("CLICKHOUSE_VERIFY", "true").lower() in {"1", "true", "yes"},
    "connect_timeout": int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "10")),
    "send_receive_timeout": int(os.getenv("CLICKHOUSE_REQUEST_TIMEOUT", "180")),
}
CLICKHOUSE_BATCH_SIZE = int(os.getenv("CLICKHOUSE_BATCH_SIZE", "50000"))
CLICKHOUSE_DEDUPLICATION_WINDOW = int(os.getenv("CLICKHOUSE_DEDUPLICATION_WINDOW", "10000"))
CLICKHOUSE_QUERY_SETTINGS = {
    "max_threads": int(os.getenv("CLICKHOUSE_MAX_THREADS", "8")),
    "max_memory_usage": int(os.getenv("CLICKHOUSE_MAX_MEMORY_BYTES", "8589934592")),
    "max_bytes_before_external_group_by": int(os.getenv("CLICKHOUSE_EXTERNAL_GROUP_BY_BYTES", "2147483648")),
}
if any(value < 1 for value in (
    CLICKHOUSE_BATCH_SIZE, CLICKHOUSE_DEDUPLICATION_WINDOW,
    CLICKHOUSE["port"], CLICKHOUSE["connect_timeout"], CLICKHOUSE["send_receive_timeout"],
    *CLICKHOUSE_QUERY_SETTINGS.values(),
)):
    raise ValueError("ClickHouse sizes, limits and timeouts must be positive")
if ELASTICSEARCH_INDEX_SOURCE_BATCH_SIZE < 1 or ELASTICSEARCH_BULK_CHUNK_SIZE < 1:
    raise ValueError("Elasticsearch source batch and bulk chunk sizes must be positive")

# Custom name of the Elasticsearch index in development / production / testing 
# https://django-elasticsearch-dsl-drf.readthedocs.io/en/latest/quick_start.html#index-definition
#ELASTICSEARCH_INDEX_NAMES = {}


# ======================================================
# =====          MICROSERVICES SETTINGS            =====
# ======================================================

# 30 mins timeout for each request (big to avoid timeouts but finite to avoid infinit waits)
MAX_TIMEOUT_SERVICES = 1800 

# ==================================================================
# =====        DURABLE NER PROCESSING SETTINGS               ======
# ==================================================================
# These defaults intentionally favour bounded, durable throughput.  They are
# settings rather than hard-coded ceilings, so operational tuning never needs a
# code change.
NER_BATCH_SIZE = int(os.getenv("NER_BATCH_SIZE", "10000"))
NER_EXTRACT_MAX_INFLIGHT = int(os.getenv("NER_EXTRACT_MAX_INFLIGHT", "2"))
NER_BACKFILL_MAX_INFLIGHT = int(os.getenv("NER_BACKFILL_MAX_INFLIGHT", "1"))
NER_HTTP_MAX_CONCURRENCY = int(os.getenv("NER_HTTP_MAX_CONCURRENCY", "2"))
NER_HTTP_TIMEOUT_SECONDS = int(os.getenv("NER_HTTP_TIMEOUT_SECONDS", "300"))
NER_EXTRACT_SOFT_TIME_LIMIT = int(os.getenv("NER_EXTRACT_SOFT_TIME_LIMIT", "900"))
NER_EXTRACT_TIME_LIMIT = int(os.getenv("NER_EXTRACT_TIME_LIMIT", "1020"))
NER_EXTRACT_LEASE_SECONDS = int(os.getenv("NER_EXTRACT_LEASE_SECONDS", "1200"))
NER_STORE_SOFT_TIME_LIMIT = int(os.getenv("NER_STORE_SOFT_TIME_LIMIT", "900"))
NER_STORE_TIME_LIMIT = int(os.getenv("NER_STORE_TIME_LIMIT", "1020"))
NER_STORE_LEASE_SECONDS = int(os.getenv("NER_STORE_LEASE_SECONDS", "1200"))
PROCESSING_BATCH_LEASE_SECONDS = int(os.getenv("PROCESSING_BATCH_LEASE_SECONDS", "1200"))
NER_EXTRACT_MAX_RETRIES = int(os.getenv("NER_EXTRACT_MAX_RETRIES", "3"))
NER_STORE_MAX_RETRIES = int(os.getenv("NER_STORE_MAX_RETRIES", "10"))
NER_RECONCILE_INTERVAL_SECONDS = int(os.getenv("NER_RECONCILE_INTERVAL_SECONDS", "60"))
NER_METADATA_TIMEOUT_SECONDS = int(os.getenv("NER_METADATA_TIMEOUT_SECONDS", "20"))

INDEX_MANUAL_BATCH_SIZE = int(os.getenv("INDEX_MANUAL_BATCH_SIZE", "100000"))
INDEX_MAX_INFLIGHT = int(os.getenv("INDEX_MAX_INFLIGHT", "1"))
INDEX_MAX_RETRIES = int(os.getenv("INDEX_MAX_RETRIES", "3"))
INDEX_SOFT_TIME_LIMIT = int(os.getenv("INDEX_SOFT_TIME_LIMIT", "3600"))
INDEX_TIME_LIMIT = int(os.getenv("INDEX_TIME_LIMIT", "3900"))
INDEX_LEASE_SECONDS = int(os.getenv("INDEX_LEASE_SECONDS", "4200"))

TELEGRAM_SCAN_ROOMS_PER_BATCH = int(os.getenv("TELEGRAM_SCAN_ROOMS_PER_BATCH", "300"))
TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH = int(os.getenv("TELEGRAM_SCAN_COMMENTS_MESSAGES_PER_BATCH", "300"))
TELEGRAM_SCAN_MAX_INFLIGHT = int(os.getenv("TELEGRAM_SCAN_MAX_INFLIGHT", "4"))
TELEGRAM_ROOM_METADATA_ROOMS_PER_BATCH = int(os.getenv("TELEGRAM_ROOM_METADATA_ROOMS_PER_BATCH", "20"))
TELEGRAM_ROOM_METADATA_MAX_INFLIGHT = int(os.getenv("TELEGRAM_ROOM_METADATA_MAX_INFLIGHT", "2"))
TELEGRAM_SCAN_MAX_RETRIES = int(os.getenv("TELEGRAM_SCAN_MAX_RETRIES", "3"))
TELEGRAM_SCAN_SOFT_TIME_LIMIT = int(os.getenv("TELEGRAM_SCAN_SOFT_TIME_LIMIT", "7200"))
TELEGRAM_SCAN_TIME_LIMIT = int(os.getenv("TELEGRAM_SCAN_TIME_LIMIT", "7800"))
TELEGRAM_SCAN_LEASE_SECONDS = int(os.getenv("TELEGRAM_SCAN_LEASE_SECONDS", "8400"))
TELEGRAM_POPULATE_SEEDS_PER_BATCH = int(os.getenv("TELEGRAM_POPULATE_SEEDS_PER_BATCH", "200"))
TELEGRAM_POPULATE_MAX_INFLIGHT = int(os.getenv("TELEGRAM_POPULATE_MAX_INFLIGHT", "4"))
TELEGRAM_POPULATE_SOFT_TIME_LIMIT = int(os.getenv("TELEGRAM_POPULATE_SOFT_TIME_LIMIT", "3600"))
TELEGRAM_POPULATE_TIME_LIMIT = int(os.getenv("TELEGRAM_POPULATE_TIME_LIMIT", "3900"))
TELEGRAM_POPULATE_LEASE_SECONDS = int(os.getenv("TELEGRAM_POPULATE_LEASE_SECONDS", "4200"))
TELEGRAM_ACCESS_ROOMS_PER_BATCH = int(os.getenv("TELEGRAM_ACCESS_ROOMS_PER_BATCH", "200"))
TELEGRAM_ACCESS_MAX_INFLIGHT = int(os.getenv("TELEGRAM_ACCESS_MAX_INFLIGHT", "4"))
TELEGRAM_ACCESS_MAX_RETRIES = int(os.getenv("TELEGRAM_ACCESS_MAX_RETRIES", "3"))
TELEGRAM_ACCESS_SOFT_TIME_LIMIT = int(os.getenv("TELEGRAM_ACCESS_SOFT_TIME_LIMIT", "3600"))
TELEGRAM_ACCESS_TIME_LIMIT = int(os.getenv("TELEGRAM_ACCESS_TIME_LIMIT", "3900"))
TELEGRAM_ACCESS_LEASE_SECONDS = int(os.getenv("TELEGRAM_ACCESS_LEASE_SECONDS", "4200"))

EMBEDDINGS_SOURCE_READ_SIZE = int(os.getenv("EMBEDDINGS_SOURCE_READ_SIZE", "100000"))
EMBEDDINGS_BATCH_SIZE = int(os.getenv("EMBEDDINGS_BATCH_SIZE", "10000"))
SENTIMENT_SOURCE_READ_SIZE = int(os.getenv("SENTIMENT_SOURCE_READ_SIZE", "500000"))
SENTIMENT_BATCH_SIZE = int(os.getenv("SENTIMENT_BATCH_SIZE", "10000"))
METADATA_MAX_INFLIGHT = int(os.getenv("METADATA_MAX_INFLIGHT", "1"))
METADATA_MAX_RETRIES = int(os.getenv("METADATA_MAX_RETRIES", "3"))
METADATA_SOFT_TIME_LIMIT = int(os.getenv("METADATA_SOFT_TIME_LIMIT", "3600"))
METADATA_TIME_LIMIT = int(os.getenv("METADATA_TIME_LIMIT", "3900"))
METADATA_LEASE_SECONDS = int(os.getenv("METADATA_LEASE_SECONDS", "4200"))

# The scheduler knows only these opaque keys. The task implementations remain
# in their owning applications, making later embedding/sentiment workflows a
# registry extension rather than an NER-specific scheduler change.
PROCESSING_STAGE_TASKS = {
    TELEGRAM_ROOM_METADATA_STAGE: {
        "task": "app_telegram.tasks.sync_room_metadata_batch", "queue": "index-q",
        "lease_seconds": INDEX_LEASE_SECONDS,
    },
    TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE: {
        "task": "app_telegram.tasks.sync_message_entity_index_batch", "queue": "index-q",
        "lease_seconds": INDEX_LEASE_SECONDS,
    },
    NER_EXTRACT_STAGE: {
        "task": "app_entity.tasks.extract_ner_batch",
        "queues": {"live": "ner-live-q", "backfill": "ner-backfill-q"},
        "lease_seconds": NER_EXTRACT_LEASE_SECONDS,
    },
    NER_STORE_STAGE: {
        "task": "app_entity.tasks.store_ner_batch", "queue": "ner-store-q",
        "lease_seconds": NER_STORE_LEASE_SECONDS,
    },
    TELEGRAM_SCAN_STAGE: {
        "task": "app_telegram.tasks.scan_rooms_processing_batch", "queue": "telegram-scan-q",
        "lease_seconds": TELEGRAM_SCAN_LEASE_SECONDS,
    },
    TELEGRAM_SCAN_COMMENTS_STAGE: {
        "task": "app_telegram.tasks.scan_comments_processing_batch", "queue": "telegram-scan-comments-q",
        "lease_seconds": TELEGRAM_SCAN_LEASE_SECONDS,
    },
    TELEGRAM_POPULATE_STAGE: {
        "task": "app_telegram.tasks.populate_processing_batch", "queue": "telegram-populate-q",
        "lease_seconds": TELEGRAM_POPULATE_LEASE_SECONDS,
    },
    # Access recovery uses Telegram's ResolveUsername API like Populate, so it
    # shares that dedicated low-concurrency worker queue.
    TELEGRAM_ACCESS_STAGE: {
        "task": "app_telegram.tasks.access_rooms_processing_batch", "queue": "telegram-populate-q",
        "lease_seconds": TELEGRAM_ACCESS_LEASE_SECONDS,
    },
    TELEGRAM_INDEX_STAGE: {
        "task": "app_telegram.tasks.index_messages_processing_batch", "queue": "index-q",
        "lease_seconds": INDEX_LEASE_SECONDS,
    },
    TELEGRAM_INDEX_ROOMS_STAGE: {
        "task": "app_telegram.tasks.index_rooms_from_seeds_processing_batch", "queue": "index-q",
        "lease_seconds": INDEX_LEASE_SECONDS,
    },
    EMBEDDINGS_STAGE: {
        "task": "app_metadata.tasks.embed_processing_batch", "queue": "embed-q",
        "lease_seconds": METADATA_LEASE_SECONDS,
    },
    SENTIMENT_STAGE: {
        "task": "app_metadata.tasks.sentiment_processing_batch", "queue": "sentiment-q",
        "lease_seconds": METADATA_LEASE_SECONDS,
    },
}
PROCESSING_SOURCE_DISPATCH_TASKS = {
    NER_PROCESSOR: {"task": "app_entity.tasks.dispatch_ner_backfill_batches", "queue": "default"},
    TELEGRAM_INDEX_MESSAGES_PROCESSOR: {"task": "app_telegram.tasks.dispatch_index_backfill_batches", "queue": "default"},
    TELEGRAM_SCAN_COMMENTS_PROCESSOR: {"task": "app_telegram.tasks.dispatch_scan_comments_source_batches", "queue": "default"},
    EMBEDDINGS_PROCESSOR: {"task": "app_metadata.tasks.dispatch_embeddings_backfill_batches", "queue": "default"},
    SENTIMENT_PROCESSOR: {"task": "app_metadata.tasks.dispatch_sentiment_backfill_batches", "queue": "default"},
}

# DatabaseScheduler merges this entry into django-celery-beat's persistent
# schedule.  It is the safety net for expired worker leases and missed publishes.
CELERY_BEAT_SCHEDULE = {
    "reconcile-durable-processing-jobs": {
        "task": "app_scheduler.tasks.reconcile_processing_jobs",
        "schedule": NER_RECONCILE_INTERVAL_SECONDS,
    },
}

NER_SERVICE = NERServiceConfig(
    host=f"http://{os.getenv('NER_SERVICE_HOST')}:{os.getenv('NER_SERVICE_PORT')}/ner",
    auth=ServiceAuthConfig(
        api_key=os.getenv("NER_SERVICE_API_KEY"),
    ),
    # Available languages (in ISO-639-1)
    languages=("en", "es"),
    endpoints={
        # Endpoint name : ServiceEndpointConfig(path=..., response_label=...)
        #
        # Request payload:
        #   {"lang": "es", "types": ["PER", "ORG", ...], "data": [
        #       {"text": "...", "id": "<pk or null>"},
        #       ...
        #   ]}
        #
        # Response (list of dicts, one per input text):
        #   [
        #       {
        #           "id": "<pk>" | null,
        #           "entities": [
        #               {"name": "...", "type": "PER", "start_offset": 0, "end_offset": 5},
        #               ...
        #           ]
        #       },
        #       ...
        #   ]
        #
        # NOTE: Response is a list (not a dict), DEFAULT_RETURN_FORMAT must be `list`.
        "ner": ServiceEndpointConfig(path="/ner", response_label="entities"),
        "metadata": ServiceEndpointConfig(path="/metadata", response_label="metadata"),
    },
    max_items_by_request=int(os.getenv("NER_SERVICE_MAX_DATA_BY_REQUEST", "2000")),

    # Recommendation: (nºdocker containers) * (nºrequest by container)
    # Depends on the server capabilities, is not recommended to overload (worst performance)
    max_parallel_requests=2*4,

    # Type of the default_return when a request fails (used by `call_service`).
    # list: returns [None] * N  |  dict: returns {label: [None] * N}
    # NER API returns a list of entity packs (each pack from a text), so the error 
    # fallback must also be a list.
    default_return_format=list,
)


OPENAI_EMBEDDINGS = OpenAIEmbeddingsConfig(
    base_url=os.getenv("OPENAI_EMBEDDINGS_BASE_URL", "https://api.openai.com/v1"),
    model=os.getenv("OPENAI_EMBEDDINGS_MODEL", "text-embedding-3-large"),
    api_key=os.getenv("OPENAI_EMBEDDINGS_API_KEY") or None,
    dimensions=(
        int(os.getenv("OPENAI_EMBEDDINGS_DIMENSIONS"))
        if os.getenv("OPENAI_EMBEDDINGS_DIMENSIONS")
        else None
    ),
    max_items_by_request=int(os.getenv("OPENAI_EMBEDDINGS_MAX_DATA_BY_REQUEST", "80")),
    max_parallel_requests=int(os.getenv("OPENAI_EMBEDDINGS_MAX_PARALLEL_REQUESTS", "12")),
    timeout=int(os.getenv("OPENAI_EMBEDDINGS_TIMEOUT", "60")),
)

EMBEDDINGS_PENDING_TIMEOUT_SECONDS = int(
    os.getenv("EMBEDDINGS_PENDING_TIMEOUT_SECONDS", "900")
)


def _get_optional_int(env_var_name):
    raw_value = os.getenv(env_var_name)
    if raw_value in (None, ""):
        return None
    return int(raw_value)


def _get_optional_bool(env_var_name):
    raw_value = os.getenv(env_var_name)
    if raw_value in (None, ""):
        return None
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_bool_or_path(env_var_name, default=False):
    raw_value = os.getenv(env_var_name)
    if raw_value in (None, ""):
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return raw_value


QDRANT = QdrantConfig(
    url=os.getenv("QDRANT_URL", "http://localhost:6333"),
    api_key=os.getenv("QDRANT_API_KEY") or None,
    timeout=int(os.getenv("QDRANT_TIMEOUT", "60")),
    search_hnsw_ef=_get_optional_int("QDRANT_SEARCH_HNSW_EF"),
    search_exact=_get_optional_bool("QDRANT_SEARCH_EXACT"),
    hnsw_m=_get_optional_int("QDRANT_HNSW_M"),
    hnsw_ef_construct=_get_optional_int("QDRANT_HNSW_EF_CONSTRUCT"),
    hnsw_full_scan_threshold=_get_optional_int("QDRANT_HNSW_FULL_SCAN_THRESHOLD"),
    upsert_batch_size=int(os.getenv("QDRANT_UPSERT_BATCH_SIZE", "1000")),
    upsert_max_retries=int(os.getenv("QDRANT_UPSERT_MAX_RETRIES", "2")),
    upsert_retry_delay_seconds=int(os.getenv("QDRANT_UPSERT_RETRY_DELAY_SECONDS", "2")),
    collections={
        "message_search": os.getenv(
            "QDRANT_COLLECTION_MESSAGE_SEARCH",
            "msg_search_embeddings"
        ),
        "categorization": os.getenv(
            "QDRANT_COLLECTION_CATEGORIZATION",
            "categorization_embeddings"
        ),
    }
)


MEDIA_S3 = {
    "endpoint_url": os.getenv("MEDIA_S3_ENDPOINT_URL", "http://localhost:9000"),
    "public_endpoint_url": os.getenv("MEDIA_S3_PUBLIC_ENDPOINT_URL", "http://localhost:9000"),
    "region": os.getenv("MEDIA_S3_REGION", "us-east-1"),
    "bucket": os.getenv("MEDIA_S3_BUCKET", "atenea-telegram-media"),
    "access_key": os.getenv("MEDIA_S3_ACCESS_KEY", "minioadmin"),
    "secret_key": os.getenv("MEDIA_S3_SECRET_KEY", "minioadmin"),
    "addressing_style": os.getenv("MEDIA_S3_ADDRESSING_STYLE", "path"),
    "use_ssl": os.getenv("MEDIA_S3_USE_SSL", "false").strip().lower() in {"1", "true", "yes", "y", "on"},
    "verify_ssl": _get_bool_or_path("MEDIA_S3_VERIFY_SSL", False),
    "presigned_ttl_seconds": int(os.getenv("MEDIA_S3_PRESIGNED_TTL_SECONDS", "900")),
    "max_file_size_bytes": int(os.getenv("MEDIA_S3_MAX_FILE_SIZE_BYTES", "52428800")),
    "create_bucket": os.getenv("MEDIA_S3_CREATE_BUCKET", "true").strip().lower() in {"1", "true", "yes", "y", "on"},
}

MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS = int(
    os.getenv("MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS", "86400")
)
MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD = int(
    os.getenv("MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD", "25")
)

MEDIA_EXTERNAL_URL_WHITELIST = [
    "drive.google.com",
    "docs.google.com",
    "storage.googleapis.com",
    "dropbox.com",
    "dl.dropboxusercontent.com",
    "onedrive.live.com",
    "1drv.ms",
    "sharepoint.com",
    "mega.nz",
    "mega.co.nz",
    "mediafire.com",
    "wetransfer.com",
    "we.tl",
    "box.com",
    "app.box.com",
    "icloud.com",
    "pcloud.com",
    "my.pcloud.com",
    "sync.com",
    "icedrive.net",
    "terabox.com",
    "4shared.com",
    "sendspace.com",
    "gofile.io",
    "file.io",
    "files.fm",
    "pixeldrain.com",
    "catbox.moe",
    "litterbox.catbox.moe",
    "krakenfiles.com",
    "workupload.com",
    "filetransfer.io",
    "transfer.sh",
    "ufile.io",
    "dropmefiles.com",
    "disk.yandex.com",
    "disk.yandex.ru",
    "cloud.mail.ru",
    "github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
    "bitbucket.org",
    "archive.org",
    "ipfs.io",
    "cloudflare-ipfs.com",
    "nextcloud.*",
    "owncloud.*",
] + [
    domain.strip().lower()
    for domain in os.getenv("MEDIA_EXTERNAL_URL_WHITELIST_EXTRA", "").split(",")
    if domain.strip()
]


SENTIMENT_SERVICE = BaseServiceConfig(
    host=f"http://{os.getenv('SENTIMENT_SERVICE_HOST')}:{os.getenv('SENTIMENT_SERVICE_PORT')}/sentiment/v1",
    auth=ServiceAuthConfig(
        api_key=os.getenv("SENTIMENT_SERVICE_API_KEY"),
    ),
    endpoints={
        # Endpoint name : ServiceEndpointConfig(path=..., response_label=...)
        #
        # Request payload:
        #   {"data": [
        #       {"text": "<text>"},
        #       ...
        #   ]}
        #
        # Response (dict with model info + sentiments list):
        #   {
        #       "model": "<model_name>",
        #       "version": "<version>",
        #       "sentiments": [0, 1, 2, ...]   <- int per input text
        #   }
        #
        # NOTE: Response is a dict. DEFAULT_RETURN_FORMAT must be `dict`.
        "sentiment": ServiceEndpointConfig(path="/sentiment", response_label="sentiments")
    },
    max_items_by_request=int(os.getenv("SENTIMENT_SERVICE_MAX_DATA_BY_REQUEST")),

    # Recommendation: (nºdocker containers) * (nºrequest by container)
    # Depends on the server capabilities, is not recommended to overload (worst performance)
    max_parallel_requests=6*2,

    # Type of the default_return when a request fails (used by `call_service`).
    # list: returns [None] * N  |  dict: returns {label: [None] * N}
    default_return_format=dict,
)



# ======================================================
# =====            LOGGING CONFIGURATION           =====
# ======================================================

LOGS_DIR = os.path.join(BASE_DIR, '.logs') 
# create log folder if doesn't exist yet
Path(LOGS_DIR).mkdir(exist_ok=True)

MAX_LOGFILE_BYTES = 1024*1024*10 # 10MB
MAX_LOGFILE_BACKUPS = 10

class _OnlyAllowDebug(logging.Filter):
     def filter(self, record):
        return record.levelno == logging.DEBUG

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "full_data": {
            "format": "[{asctime:s}][{levelname}][{threadName}:{thread:d}][{process:d}][{name}][{filename}][{module}.{funcName}:{lineno}][{message}]",
            "style": "{",
        },
        "mid_data": {
            "format": "[{asctime:s}][{levelname}][{name}][{module}.{funcName}:{lineno}][{message}]",
            "style": "{",
        },
    },
    "filters": {
        "debug_only_filter": {
            "()": _OnlyAllowDebug,
        },
    },
    "handlers": {
        "console": {
            "level": os.getenv("DJANGO_LOG_LEVEL", "WARNING"),
            "class": "logging.StreamHandler",
            "formatter": "mid_data",
        },
        "console_INFO": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "mid_data",
        },
        "project_file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "maxBytes" : MAX_LOGFILE_BYTES,
            "backupCount" : MAX_LOGFILE_BACKUPS,
            "filename": os.path.join(LOGS_DIR, "project.log"),
            "formatter": "full_data",
        },
        "project_debug_file": {
            "level": "DEBUG",
            "class": "logging.handlers.RotatingFileHandler",
            "maxBytes" : MAX_LOGFILE_BYTES,
            "backupCount" : MAX_LOGFILE_BACKUPS,
            "filename": os.path.join(LOGS_DIR, "project_debug.log"),
            "formatter": "full_data",
            "filters": ["debug_only_filter"],
        },
        "celery_tasks_file" : {
            "level" : "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "maxBytes" : MAX_LOGFILE_BYTES, 
            "backupCount" : MAX_LOGFILE_BACKUPS,
            "filename": os.path.join(LOGS_DIR, "celery_tasks.log"),
            "formatter" : "full_data",
        },
        "celery_file" : {
            "level" : "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "maxBytes" : MAX_LOGFILE_BYTES, 
            "backupCount" : MAX_LOGFILE_BACKUPS,
            "filename": os.path.join(LOGS_DIR, "celery.log"),
            "formatter" : "full_data",
        },
        "elasticsearch_file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "maxBytes" : MAX_LOGFILE_BYTES,
            "backupCount" : MAX_LOGFILE_BACKUPS,
            "filename": os.path.join(LOGS_DIR, "elasticsearch.log"),
            "formatter": "full_data",
        },
        "django_file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "maxBytes" : MAX_LOGFILE_BYTES,
            "backupCount" : MAX_LOGFILE_BACKUPS,
            "filename": os.path.join(LOGS_DIR, "django.log"),
            "formatter": "full_data",
        },
    },
    "root": { # Default logger configuration
        "handlers": ["console", "project_file"]
        + (
            ["project_debug_file"]
            if os.getenv("DJANGO_LOG_LEVEL") == "DEBUG"
            else []
        ),
        "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
        "propagate": True,
    },
    "loggers": { # Specific logger configuration 
        # 'propagate=False' : The logs will only be captured by that logger and 
        # will not be sent to the parent 
        "celery" : {
            "handlers" : ["console_INFO", "celery_file"],
            "propagate" : False,
            "level" : "INFO",
        },
        "celery.app.trace" : {
            "handlers" : ["console_INFO", "celery_tasks_file"],
            "propagate" : False,
            "level" : "INFO",
        },
        "celery.worker.strategy" : {
            "handlers" : ["console_INFO", "celery_tasks_file"],
            "propagate" : False,
            "level" : "INFO",
        },
        "elasticsearch": {
            "handlers": ["elasticsearch_file"],
            "propagate": False,
            "level": "INFO",
        },
        "elastic_transport": {
            "handlers": ["elasticsearch_file"],
            "propagate": False,
            "level": "INFO",
        },
        "django": {
            "handlers": ["django_file"],
            "propagate": False,
            "level": "INFO",
        },
        "django.server": {
            "handlers": ["console_INFO"],
            "propagate": True,
            "level": "INFO",
        },
    },
}



# ======================================================
# =====      EXTRA GLOBAL VARIABLES SETTINGS       =====
# ======================================================

# (for app_base.utils.convert_to_standard_text) 
# Define Unicode ranges and their corresponding offsets
RANGES_OFFSETS = {
    (0x1D400, 0x1D419): ord('A'),  # Bold capitals
    (0x1D41A, 0x1D433): ord('a'),  # Bold lowercase
    (0x1D434, 0x1D44D): ord('A'),  # Italic capitals
    (0x1D44E, 0x1D467): ord('a'),  # Italic lowercase
    (0x1D468, 0x1D481): ord('A'),  # Bold italic capitals
    (0x1D482, 0x1D49B): ord('a'),  # Bold italic lowercase
    (0x1D49C, 0x1D4B5): ord('A'),  # Script capitals
    (0x1D4B6, 0x1D4CF): ord('a'),  # Script lowercase
    (0x1D504, 0x1D51D): ord('A'),  # Fraktur capitals
    (0x1D51E, 0x1D537): ord('a'),  # Fraktur lowercase
    (0x1D538, 0x1D551): ord('A'),  # Double-struck capitals
    # Some characters missing in Double-struck lowercase, hence not included
    (0x1D56C, 0x1D585): ord('A'),  # Bold Fraktur capitals
    (0x1D586, 0x1D59F): ord('a'),  # Bold Fraktur lowercase
    (0x1D5A0, 0x1D5B9): ord('A'),  # Sans-serif capitals
    (0x1D5BA, 0x1D5D3): ord('a'),  # Sans-serif lowercase
    (0x1D5D4, 0x1D5ED): ord('A'),  # Sans-serif Bold capitals
    (0x1D5EE, 0x1D607): ord('a'),  # Sans-serif Bold lowercase
    (0x1D608, 0x1D621): ord('A'),  # Sans-serif Italic capitals
    (0x1D622, 0x1D63B): ord('a'),  # Sans-serif Italic lowercase
    (0x1D63C, 0x1D655): ord('A'),  # Sans-serif Bold Italic capitals
    (0x1D656, 0x1D66F): ord('a'),  # Sans-serif Bold Italic lowercase
    (0x1D670, 0x1D689): ord('A'),  # Monospace capitals
    (0x1D68A, 0x1D6A3): ord('a'),  # Monospace lowercase
    (0x1D4D0, 0x1D4E9): ord('A'),  # Bold Script capitals
    # No direct mapping for lower case in "Bold Script".
    # Add other ranges as needed...
}
UNICODE_TO_STANDARD_MAP = {
    code_point: chr(offset + (code_point - start))
    for (start, end), offset in RANGES_OFFSETS.items()
    for code_point in range(start, end+1)
}
