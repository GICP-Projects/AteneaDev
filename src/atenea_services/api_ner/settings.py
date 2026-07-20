"""
This module contains configuration settings and constants for the NER api service. 
"""
import os
from pathlib import Path
import tomllib


with (Path(__file__).resolve().parent / "pyproject.toml").open("rb") as pyproject_file:
    PROJECT_VERSION = tomllib.load(pyproject_file)["project"]["version"]


# All spaCy models available for each allowed language (lang code must use ISO-639-1).
# Model packages and versions are pinned in pyproject.toml and uv.lock.
ALLOWED_LANG_MODELS = {
    # LANG: (SPACY MODEL, DISABLE PIPELINES)
    "es": ("es_core_news_lg", []),  # Fast model: around 2 seconds for 480 items
    "en": ("en_core_web_lg", [])

    # TRF model, not recommended with CPU (slower and expensive for 2-3% improvement)
    # https://stackoverflow.com/a/66634539 (2 secs improvement 30 secs -> 28secs for 480 items)
    #"en": ("en_core_web_trf", ["tagger", "parser", "attribute_ruler", "lemmatizer"])
}

# ANY ALLOWED API-KEY MUST BE STORED HERE
# TODO: encrypt the key and add a step to decrypt the key 
_API_KEY = os.getenv("NER_SERVICE_API_KEY")
ALLOWED_API_KEYS = [_API_KEY] if _API_KEY else []

# Max number of texts allowed in each request
MAX_DATA_BY_REQUEST = int(os.getenv("MAX_DATA_BY_REQUEST", 100)) 

# LOGGER CONFIGURATION
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "[{asctime}][{levelname}][{name}][{funcName}:{lineno}][{message}]",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "default",
        },
    },
    "root": {
        "handlers": ["console"], 
        "level": "DEBUG",
        "formatters": "default",
        "propagate": True,
    },
}
