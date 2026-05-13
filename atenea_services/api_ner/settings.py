"""
This module contains configuration settings and constants for the NER api service. 
"""
import os


# All spacy models available for each allowed language (lang code must use ISO-639-1). 
# NOTE: Each used model must be added to the downloader script (download.sh)
ALLOWED_LANG_MODELS = {
    # LANG: (SPACY MODEL, DISABLE PIPELINES)
    "es": ("es_core_news_lg", []), # Unnecesary, very fast model 2secs for 480 items
    "en": ("en_core_web_lg", [])

    # TRF model, not recommended with CPU (slower and expensive for 2-3% improvement)
    # https://stackoverflow.com/a/66634539 (2 secs improvement 30 secs -> 28secs for 480 items)
    #"en": ("en_core_web_trf", ["tagger", "parser", "attribute_ruler", "lemmatizer"])
}

# ANY ALLOWED API-KEY MUST BE STORED HERE
# TODO: encrypt the key and add a step to decrypt the key 
ALLOWED_API_KEYS = [
    os.getenv("NER_SERVICE_API_KEY")
]

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