import logging
from settings import (
    AVAILABLE_MODELS, 
    LOGGING_CONFIG, 
    ALLOWED_API_KEYS, 
    ROOT_PATH
)
from fastapi import FastAPI, HTTPException, Security, BackgroundTasks
from fastapi.security.api_key import APIKeyHeader
from starlette.status import HTTP_403_FORBIDDEN, HTTP_503_SERVICE_UNAVAILABLE
from models.sentiment import get_model_class
from src.serializers import (
    RequestSentiments,
    ResponseSentiments
)


async def _load_model():
    global SENTIMENT_API
    SENTIMENT_API = get_model_class()(*AVAILABLE_MODELS[0])
    
app = FastAPI(
    title="Rest API Model (Sentiment) 🤖",
    description="""
        Rest API service to retrieve sentiment from text.
    """,
    version="0.0.1",
    on_startup=[_load_model],
    root_path=ROOT_PATH # Allow /docs to function correctly behind a proxy
)

#TODO Make a better api versioning mecanism
API_VERSION = "/v1"

# ======================================================
# =====             GLOBAL VARIABLES               =====
# ======================================================

SENTIMENT_API = None 

API_KEY_HEADER = APIKeyHeader(name="ApiKey", auto_error=False)

# Configure logger
logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)

# ======================================================
# =====             CONFIG ENDPOINTS               =====
# ======================================================

async def get_api_key(api_key_header: str = Security(API_KEY_HEADER)):
    if api_key_header in ALLOWED_API_KEYS:
        return api_key_header   
    else:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, 
            detail="Could not validate the authentication key."
        )

@app.get(
    "/load",
    description="Endpoint to load the model. This endpoints is called at service initilization.",
)
def load(api_key: str = Security(get_api_key)):
    try:
        # Load NER MODEL
        _load_model()
        return {"return": "Load completed"}

    except KeyError as e:
        msg = "Error, settings file is not configured correctly."
        logger.error(msg + e)
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=msg)
    except Exception as e:
        msg = "The load has failed."
        logger.error(msg + e)
        raise HTTPException(status_code=404, detail=msg)


@app.get(
    "/check",
    description="Check if all needed elements have been loaded.",
)
def check(api_key: str = Security(get_api_key)):
    if not SENTIMENT_API:
        return {"return": False}
    return {"return": True}


# ======================================================
# =====                 ENDPOINTS                  =====
# ======================================================

@app.post(
    API_VERSION + "/sentiment",
    description="Endpoint to retrieve sentiment from a block of texts.",
    response_model=ResponseSentiments,
)
def sentiment(
    request: RequestSentiments, 
    background_tasks: BackgroundTasks,
    api_key: str = Security(get_api_key),
):
    results, labels = SENTIMENT_API.predict(input_texts=[item.text for item in request.data])
    background_tasks.add_task(SENTIMENT_API.clean_prediction)
    return {
        "model": SENTIMENT_API.get_name(),
        "version": SENTIMENT_API.get_version(),
        "labels": labels,
        "sentiments": results
    }
