import asyncio
import os

import pytest

from src import api


pytestmark = [
    pytest.mark.model_smoke,
    pytest.mark.skipif(
        os.getenv("RUN_MODEL_SMOKE_TESTS") != "1"
        or os.getenv("HF_HUB_OFFLINE") != "1",
        reason=(
            "Set RUN_MODEL_SMOKE_TESTS=1 and HF_HUB_OFFLINE=1 to load the "
            "configured Transformers model from the local cache"
        ),
    ),
]


def test_configured_transformers_model_can_process_text():
    asyncio.run(api._load_model())

    predictions, labels = api.SENTIMENT_API.predict(["A short smoke-test sentence."])

    assert len(predictions) == 1
    assert predictions[0] in labels
