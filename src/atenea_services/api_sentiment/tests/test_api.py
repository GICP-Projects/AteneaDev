import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from src import api


def test_openapi_version_comes_from_project_metadata():
    assert api.app.version == api.PROJECT_VERSION


def test_endpoints_reject_missing_and_invalid_api_keys(client):
    assert client.get("/check").status_code == 403
    assert client.get("/check", headers={"ApiKey": "wrong"}).status_code == 403


def test_check_reports_model_state(client, auth_headers, monkeypatch):
    assert client.get("/check", headers=auth_headers).json() == {"return": False}

    monkeypatch.setattr(api, "SENTIMENT_API", object())
    assert client.get("/check", headers=auth_headers).json() == {"return": True}


def test_load_waits_for_model_initialization(client, auth_headers, monkeypatch):
    load_model = AsyncMock()
    monkeypatch.setattr(api, "_load_model", load_model)

    response = client.get("/load", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"return": "Load completed"}
    load_model.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(KeyError("bad settings"), 500), (RuntimeError("model unavailable"), 503)],
)
def test_load_translates_initialization_errors(
    client,
    auth_headers,
    monkeypatch,
    error,
    expected_status,
):
    monkeypatch.setattr(api, "_load_model", AsyncMock(side_effect=error))

    response = client.get("/load", headers=auth_headers)

    assert response.status_code == expected_status


def test_load_model_uses_configured_name_and_length(monkeypatch):
    instance = object()
    constructor = Mock(return_value=instance)
    monkeypatch.setattr(api, "AVAILABLE_MODELS", [("configured-model", 256)])
    monkeypatch.setattr(api, "get_model_class", Mock(return_value=constructor))

    asyncio.run(api._load_model())

    constructor.assert_called_once_with("configured-model", 256)
    assert api.SENTIMENT_API is instance


@pytest.mark.parametrize(
    "configuration",
    [[(None, 128)], [("model", 0)]],
)
def test_load_model_rejects_invalid_configuration(monkeypatch, configuration):
    monkeypatch.setattr(api, "AVAILABLE_MODELS", configuration)

    with pytest.raises(KeyError):
        asyncio.run(api._load_model())


class FakeSentimentModel:
    def __init__(self):
        self.inputs = None
        self.cleaned = False

    def predict(self, input_texts):
        self.inputs = input_texts
        return [2, 0], {0: "negative", 2: "positive"}

    def clean_prediction(self):
        self.cleaned = True

    def get_name(self):
        return "fake-sentiment"

    def get_version(self):
        return "1.0"


def test_sentiment_endpoint_returns_ordered_results_and_cleans_up(
    client,
    auth_headers,
    monkeypatch,
):
    model = FakeSentimentModel()
    monkeypatch.setattr(api, "SENTIMENT_API", model)

    response = client.post(
        "/v1/sentiment",
        headers=auth_headers,
        json={"data": [{"text": "good"}, {"text": "bad"}]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "model": "fake-sentiment",
        "version": "1.0",
        "labels": {"0": "negative", "2": "positive"},
        "sentiments": [2, 0],
    }
    assert model.inputs == ["good", "bad"]
    assert model.cleaned is True


def test_sentiment_endpoint_returns_503_before_model_load(client, auth_headers):
    response = client.post(
        "/v1/sentiment",
        headers=auth_headers,
        json={"data": [{"text": "example"}]},
    )

    assert response.status_code == 503


@pytest.mark.parametrize("data", [[], [{"text": ""}], [{"text": "   "}]])
def test_sentiment_endpoint_rejects_empty_work(client, auth_headers, data):
    response = client.post(
        "/v1/sentiment",
        headers=auth_headers,
        json={"data": data},
    )

    assert response.status_code == 422
