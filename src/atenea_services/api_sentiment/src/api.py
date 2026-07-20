import logging
import logging.config
from settings import (
    AVAILABLE_MODELS, 
    LOGGING_CONFIG, 
    ALLOWED_API_KEYS, 
    PROJECT_VERSION,
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
    model_name, max_length = AVAILABLE_MODELS[0]
    if not model_name or max_length < 1:
        raise KeyError("MODEL_NAME and a positive MODEL_MAX_TOKENS are required")
    SENTIMENT_API = get_model_class()(model_name, max_length)
    
app = FastAPI(
    title="Rest API Model (Sentiment) 🤖",
    description="""
        Rest API service to retrieve sentiment from text.
    """,
    version=PROJECT_VERSION,
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
    if api_key_header and api_key_header in ALLOWED_API_KEYS:
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
async def load(api_key: str = Security(get_api_key)):
    try:
        # Load NER MODEL
        await _load_model()
        return {"return": "Load completed"}

    except KeyError as e:
        msg = "Error, settings file is not configured correctly."
        logger.error("%s: %s", msg, e)
        raise HTTPException(status_code=500, detail=msg)
    except Exception as e:
        msg = "The load has failed."
        logger.error("%s: %s", msg, e)
        raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=msg)


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
    if SENTIMENT_API is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="The sentiment model has not been loaded.",
        )
    results, labels = SENTIMENT_API.predict(input_texts=[item.text for item in request.data])
    background_tasks.add_task(SENTIMENT_API.clean_prediction)
    return {
        "model": SENTIMENT_API.get_name(),
        "version": SENTIMENT_API.get_version(),
        "labels": labels,
        "sentiments": results
    }
