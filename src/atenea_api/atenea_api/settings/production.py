from .base import BASE_DIR
from .base import *

# where are our URLs
ROOT_URLCONF = "atenea_api.urls"

WSGI_APPLICATION = "atenea_api.wsgi.application"

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

# site in Django admin > Sites
SITE_ID = 1


# Deployment-specific hosts/IPs go in DJANGO_EXTRA_ALLOWED_HOSTS (comma-separated).
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    *env_list("DJANGO_EXTRA_ALLOWED_HOSTS"),
]


# ======================================================
# =====             CORS CONNECTION                =====
# ======================================================
CORS_ALLOW_CREDENTIALS = True
# Allowed front-server origins; add deployment-specific ones in
# DJANGO_EXTRA_CORS_ORIGINS (comma-separated, e.g. "http://host:3001").
CORS_ORIGIN_WHITELIST = (
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    *env_list("DJANGO_EXTRA_CORS_ORIGINS"),
)
CORS_ALLOW_HEADERS = [
    "front-api-key",
    "authorization"
]


# ======================================================
# =====         ELASTICSEARCH SETTINGS             =====
# ======================================================
suffix = os.getenv("DEPLOYMENT_UNIQUE_SUFFIX", "")
CLICKHOUSE["host"] = os.getenv("CLICKHOUSE_HOST", "atenea-clickhouse")
CLICKHOUSE["port"] = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE["database"] = os.getenv("CLICKHOUSE_DATABASE") or f"atenea_prod{f'_{suffix}' if suffix else ''}"
# Name of the Elasticsearch index in development / production / testing 
ELASTICSEARCH_INDEX_NAMES = {
    'app_telegram.documents.RoomDocument': f'prod_room{f"_{suffix}" if suffix else ""}',
    'app_telegram.documents.MessageDocument': f'prod_msg{f"_{suffix}" if suffix else ""}',
}


# ======================================================
# =====             QDRANT SETTINGS                =====
# ======================================================
QDRANT.collections["message_search"] = (
    f'prod_{QDRANT.collections["message_search"]}{f"_{suffix}" if suffix else ""}'
)
QDRANT.collections["categorization"] = (
    f'prod_{QDRANT.collections["categorization"]}{f"_{suffix}" if suffix else ""}'
)
