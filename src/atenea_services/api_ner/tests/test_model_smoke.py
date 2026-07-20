import asyncio
import os

import pytest

from models.ner import normalize_entity_type
from settings import ALLOWED_LANG_MODELS
from src import api
from src.serializers import NERRequestList


EXPECTED_NER_LABELS = {
    "es": {"LOC", "MISC", "ORG", "PER"},
    "en": {
        "CARDINAL",
        "DATE",
        "EVENT",
        "FAC",
        "GPE",
        "LANGUAGE",
        "LAW",
        "LOC",
        "MONEY",
        "NORP",
        "ORDINAL",
        "ORG",
        "PERCENT",
        "PERSON",
        "PRODUCT",
        "QUANTITY",
        "TIME",
        "WORK_OF_ART",
    },
}


pytestmark = [
    pytest.mark.model_smoke,
    pytest.mark.skipif(
        os.getenv("RUN_MODEL_SMOKE_TESTS") != "1",
        reason="Set RUN_MODEL_SMOKE_TESTS=1 to load the configured spaCy models",
    ),
]


def test_configured_spacy_models_can_process_text():
    asyncio.run(api._load_model())

    assert set(api.NER_API) == set(ALLOWED_LANG_MODELS)
    for language in ALLOWED_LANG_MODELS:
        request = NERRequestList(
            data=[{"id": language, "text": "Alice visited Madrid."}],
            lang=language,
        )
        result = api._ner(request)

        assert len(result) == 1
        assert result[0]["id"] == language
        assert isinstance(result[0]["entities"], list)


def test_configured_spacy_model_taxonomies_are_explicit():
    asyncio.run(api._load_model())

    assert set(ALLOWED_LANG_MODELS) == set(EXPECTED_NER_LABELS)
    for language, ner_api in api.NER_API.items():
        labels = set(ner_api.nlp.get_pipe("ner").labels)
        assert labels == EXPECTED_NER_LABELS[language]
        normalized_labels = {normalize_entity_type(label) for label in labels}
        assert {"ORG", "PER", "LOCATION"}.issubset(normalized_labels)
