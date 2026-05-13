import logging
from settings import ALLOWED_LANG_MODELS, LOGGING_CONFIG, ALLOWED_API_KEYS
from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from typing import List
from starlette.status import HTTP_403_FORBIDDEN, HTTP_503_SERVICE_UNAVAILABLE
from models.ner import NerAPI
from src.serializers import (
    NERRequestList,
    ResponseNER, 
    ResponseAnnotate
)


async def _load_model():
    global NER_API
    NER_API = {
        lang: NerAPI(spacy_info[0], spacy_info[1]) 
        for lang, spacy_info in ALLOWED_LANG_MODELS.items()
    }

    
app = FastAPI(
    title="Rest API Model (NER)",
    description="""
        Rest API service to make name entity recognition using the Spacy framework🤖.
        Allowed languages:  
        - Spanish (es)
        - English (en)
    """,
    version="0.0.1",
    on_startup=[_load_model],
)

# ======================================================
# =====             GLOBAL VARIABLES               =====
# ======================================================

NER_API = {} # keys: lang codes ISO-639-1 | values: NerAPI with Spacy models loaded

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
    description="Endpoint to pre-load any required Spacy model (currently: es_core_news_lg "
    "and en_core_web_trf). This endpoints is called at service initilization.",
)
def load(api_key: str = Security(get_api_key)):

    try:
        # Load NER MODEL
        _load_model()
        return {"return": "Load completed"}

    except KeyError as e:
        msg = "Error, settings file is not configured correctly."
        logger.error(msg + e)
        raise HTTPException(status_code=500, detail=msg)
    except Exception as e:
        msg = "The load has failed."
        logger.error(msg + e)
        raise HTTPException(status_code=404, detail=msg)


@app.get(
    "/check",
    description="Check if all needed elements have been loaded.",
)
def check(api_key: str = Security(get_api_key)):
    if not NER_API:
        return {"return": False}
    return {"return": True}


# ======================================================
# =====               NER ENDPOINTS                =====
# ======================================================

def _ner(request, annotate_text = False):
    """ Verify if the Spacy model is pre-loaded in the selected language and 
    extract entities.
    """
    if not NER_API.get(request.lang):
        logger.info(f"Language {request.lang} not supported.")
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE, 
            detail=f"The model for lang:{request.lang} has not been loaded."
        )
    results = NER_API.get(request.lang).ner(
        text_items=request.data, 
        allowed_types=request.allowed_types,
        annotate_text=annotate_text
    ) 
    return results 


@app.post(
    "/ner",
    description="Endpoint to extract entities from a given text.",
    response_model=List[ResponseNER],
)
def ner(request: NERRequestList, api_key: str = Security(get_api_key)):
    results = _ner(request)
    return results


@app.post(
    "/annotate",
    description="Endpoint that use the loaded elements to predict. "
    "In this case, predict the entities in the text",
    response_model=List[ResponseAnnotate]
)
def annotate(request: NERRequestList, api_key: str = Security(get_api_key)):
    results = _ner(request, annotate_text=True)
    return results