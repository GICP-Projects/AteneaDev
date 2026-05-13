import os
import logging
from dotenv import load_dotenv

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Default settings folder path
BASE_SETTINGS_PATH = "atenea_api.settings"

# Get the DJANGO_SETTINGS_MODULE environment variable
SETTINGS_MODULE = os.getenv('DJANGO_SETTINGS_MODULE')

if SETTINGS_MODULE == f'{BASE_SETTINGS_PATH}.production':
    logger.info("Loading production settings.")
elif SETTINGS_MODULE == f'{BASE_SETTINGS_PATH}.development':
    # Load environment variables from .env file
    logger.info("Loading development environment variables from .env.dev file...")
    load_dotenv("../.env.dev")
    logger.info("Loading development settings...")  
else:
    msg = f'Invalid `DJANGO_SETTINGS_MODULE`: "{SETTINGS_MODULE}". Must be "development" '
    'or "production". In development use `export DJANGO_SETTINGS_MODULE=atenea_api.settings.development` '
    'before running the server.'
    logger.error(msg)
    raise ValueError(msg)
