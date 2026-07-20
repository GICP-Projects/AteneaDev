from .base import BASE_DIR
from .base import *

# where are our URLs
ROOT_URLCONF = "atenea_api.urls"

WSGI_APPLICATION = "atenea_api.wsgi.application"

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

# site in Django admin > Sites
SITE_ID = 1


ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "161.111.31.220", # arwen ip 
]


# ======================================================
# =====             CORS CONNECTION                =====
# ======================================================
CORS_ALLOW_CREDENTIALS = True
CORS_ORIGIN_WHITELIST = (
    # Allowed front-server requests domains/IPs from..
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    "http://161.111.31.220:28001" # development deployed
)
CORS_ALLOW_HEADERS = [
    "front-api-key",
    "authorization"
]


# ======================================================
# =====         ELASTICSEARCH SETTINGS             =====
# ======================================================
suffix = os.getenv("DEPLOYMENT_UNIQUE_SUFFIX", "")
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
