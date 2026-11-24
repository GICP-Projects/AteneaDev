import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src import api


def test_openapi_version_comes_from_project_metadata():
    assert api.app.version == api.PROJECT_VERSION


def test_endpoints_reject_missing_and_invalid_api_keys(client):
    assert client.get("/check").status_code == 403
    assert client.get("/check", headers={"ApiKey": "wrong"}).status_code == 403


def test_check_reports_model_state(client, auth_headers, monkeypatch):
    assert client.get("/check", headers=auth_headers).json() == {"return": False}

    monkeypatch.setattr(api, "NER_API", {"en": object()})
    assert client.get("/check", headers=auth_headers).json() == {"return": True}


def test_metadata_is_protected_and_describes_configured_models(client, auth_headers, monkeypatch):
    assert client.get("/metadata").status_code == 403

    class LoadedModel:
        class nlp:
            meta = {"name": "real_es_model", "version": "3.8.0"}

    monkeypatch.setattr(api, "NER_API", {"es": LoadedModel()})
    response = client.get("/metadata", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["service_version"] == api.PROJECT_VERSION
    assert body["spacy_version"] == api.spacy.__version__
    assert body["models"]["es"]["name"] == "real_es_model"
    assert body["models"]["en"]["name"] == "en_core_web_lg"
    assert body["schema_version"] == "ner-output-v1"


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


def test_load_model_builds_every_configured_language(monkeypatch):
    constructor = Mock(side_effect=["spanish", "english"])
    monkeypatch.setattr(
        api,
        "ALLOWED_LANG_MODELS",
        {"es": ("es_model", ["parser"]), "en": ("en_model", [])},
    )
    monkeypatch.setattr(api, "NerAPI", constructor)

    asyncio.run(api._load_model())

    assert api.NER_API == {"es": "spanish", "en": "english"}
    assert constructor.call_args_list[0].args == ("es_model", ["parser"])
    assert constructor.call_args_list[1].args == ("en_model", [])


class FakeNerModel:
    def __init__(self):
        self.calls = []

    def ner(self, text_items, allowed_types, annotate_text=False):
        self.calls.append((text_items, allowed_types, annotate_text))
        results = []
        for item in text_items:
            result = {
                "id": item.id,
                "entities": [
                    {
                        "name": "Alice",
                        "type": "PERSON",
                        "start_offset": 0,
                        "end_offset": 5,
                    }
                ],
            }
            if annotate_text:
                result["annotated_text"] = "[Alice](Alice&PERSON)"
            results.append(result)
        return results


@pytest.mark.parametrize(
    ("path", "annotate"),
    [("/ner", False), ("/annotate", True)],
)
def test_ner_endpoints_forward_validated_requests(
    client,
    auth_headers,
    monkeypatch,
    path,
    annotate,
):
    model = FakeNerModel()
    monkeypatch.setattr(api, "NER_API", {"en": model})

    response = client.post(
        path,
        headers=auth_headers,
        json={
            "data": [{"id": "first", "text": "Alice"}],
            "types": ["PERSON"],
            "lang": "en",
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["id"] == "first"
    assert model.calls[0][1:] == (["PERSON"], annotate)


def test_unknown_language_is_validation_error(client, auth_headers):
    response = client.post(
        "/ner",
        headers=auth_headers,
        json={"data": [{"text": "Bonjour"}], "lang": "fr"},
    )

    assert response.status_code == 422


def test_configured_but_unloaded_language_returns_503(client, auth_headers):
    response = client.post(
        "/ner",
        headers=auth_headers,
        json={"data": [{"text": "Hello"}], "lang": "en"},
    )

    assert response.status_code == 503
