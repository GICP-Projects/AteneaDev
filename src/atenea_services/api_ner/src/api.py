import logging
import logging.config
import spacy
from settings import (
    ALLOWED_API_KEYS,
    ALLOWED_LANG_MODELS,
    LOGGING_CONFIG,
    PROJECT_VERSION,
)
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
    version=PROJECT_VERSION,
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
    if api_key_header and api_key_header in ALLOWED_API_KEYS:
        return api_key_header   
    else:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, 
            detail="Could not validate the authentication key."
        )

@app.get(
    "/load",
    description="Endpoint to pre-load any required Spacy model (currently: es_core_news_lg "
    "and en_core_web_lg). This endpoint is called at service initialization.",
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
async def check(api_key: str = Security(get_api_key)):
    if not NER_API:
        return {"return": False}
    return {"return": True}


@app.get(
    "/metadata",
    description="Return the version information Atenea records for each NER batch.",
)
async def metadata(api_key: str = Security(get_api_key)):
    """Expose model versions without exposing operational configuration.

    The endpoint uses exactly the same API-key dependency as the extraction
    endpoints.  When models are loaded, spaCy's package metadata is more useful
    than merely echoing settings because it identifies the actual loaded model.
    """

    models = {}
    for language, (configured_name, disabled) in ALLOWED_LANG_MODELS.items():
        loaded = NER_API.get(language)
        meta = getattr(getattr(loaded, "nlp", None), "meta", {}) or {}
        models[language] = {
            "name": meta.get("name") or configured_name,
            "version": meta.get("version") or "unknown",
            "disable": list(disabled),
        }

    return {
        "service_version": PROJECT_VERSION,
        "spacy_version": spacy.__version__,
        "models": models,
        "schema_version": "ner-output-v1",
    }


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
async def ner(request: NERRequestList, api_key: str = Security(get_api_key)):
    # NER is intentionally bounded by Atenea's durable dispatcher.  Keeping
    # this handler async avoids FastAPI's extra threadpool hand-off while the
    # synchronous model call remains deterministic within one API worker.
    results = _ner(request)
    return results


@app.post(
    "/annotate",
    description="Endpoint that use the loaded elements to predict. "
    "In this case, predict the entities in the text",
    response_model=List[ResponseAnnotate]
)
async def annotate(request: NERRequestList, api_key: str = Security(get_api_key)):
    results = _ner(request, annotate_text=True)
    return results
