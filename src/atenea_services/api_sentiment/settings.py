"""
This module contains configuration settings and constants for the service.
"""
import os
from pathlib import Path
import tomllib


with (Path(__file__).resolve().parent / "pyproject.toml").open("rb") as pyproject_file:
    PROJECT_VERSION = tomllib.load(pyproject_file)["project"]["version"]
#from dotenv import load_dotenv

#load_dotenv("../.env")

ROOT_PATH = os.getenv("ROOT_PATH")

# DEVICE TO DEPLOY THE MODEL
DEVICE = os.getenv("DEVICE", "cpu")

# NUMBER OF UVICORN WORKERS (1 BY DEFAULT)
UVICORN_WORKERS = int(os.getenv("UVICORN_WORKERS", 1))

# TRUST REPO CODE 
TRUST_REMOTE_CODE = (os.getenv('TRUST_REMOTE_CODE', 'False').lower() == 'true')

# ANY ALLOWED API-KEY MUST BE STORED HERE
# TODO: encrypt the key and add a step to decrypt the key 
_API_KEY = os.getenv("API_KEY")
ALLOWED_API_KEYS = [_API_KEY] if _API_KEY else []

# Available models: List of tuples with (model_name, max_tokens_length)
AVAILABLE_MODELS = [
    (os.getenv("MODEL_NAME"), int(os.getenv("MODEL_MAX_TOKENS", 1)))
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
