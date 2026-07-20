import pytest
from pydantic import ValidationError

from src.serializers import RequestSentiments


def test_request_requires_at_least_one_text():
    with pytest.raises(ValidationError):
        RequestSentiments(data=[])


@pytest.mark.parametrize("text", ["", "   ", None])
def test_request_rejects_empty_text(text):
    with pytest.raises(ValidationError):
        RequestSentiments(data=[{"text": text}])


def test_request_enforces_configured_maximum():
    with pytest.raises(ValidationError):
        RequestSentiments(data=[{"text": str(index)} for index in range(4)])


def test_request_preserves_text_and_order():
    request = RequestSentiments(data=[{"text": " first "}, {"text": "second"}])

    assert [item.text for item in request.data] == [" first ", "second"]
