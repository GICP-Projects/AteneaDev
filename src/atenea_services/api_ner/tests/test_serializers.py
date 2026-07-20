import pytest
from pydantic import ValidationError

from src.serializers import NERRequestList


def test_request_requires_at_least_one_text():
    with pytest.raises(ValidationError):
        NERRequestList(data=[], lang="en")


@pytest.mark.parametrize("text", ["", "   ", None])
def test_request_rejects_empty_text(text):
    with pytest.raises(ValidationError):
        NERRequestList(data=[{"text": text}], lang="en")


def test_request_enforces_configured_maximum():
    with pytest.raises(ValidationError):
        NERRequestList(
            data=[{"text": f"text {index}"} for index in range(4)],
            lang="en",
        )


@pytest.mark.parametrize("entity_type", ["", "   "])
def test_request_rejects_empty_entity_types(entity_type):
    with pytest.raises(ValidationError):
        NERRequestList(
            data=[{"text": "Example"}],
            lang="en",
            types=[entity_type],
        )


def test_request_rejects_unknown_language():
    with pytest.raises(ValidationError):
        NERRequestList(data=[{"text": "Bonjour"}], lang="fr")


def test_request_accepts_alias_and_preserves_text_and_ids():
    request = NERRequestList(
        data=[{"id": "msg-1", "text": "  Alice in Madrid  "}],
        lang="en",
        types=["PERSON", "GPE"],
    )

    assert request.data[0].id == "msg-1"
    assert request.data[0].text == "  Alice in Madrid  "
    assert request.allowed_types == ["PERSON", "GPE"]
