from types import SimpleNamespace
from unittest.mock import Mock, patch

import torch

from models.sentiment import SentimentAPI, SentimentPipelineAPI, get_model_class


class FakeBatch(dict):
    def __init__(self):
        super().__init__({"input_ids": torch.tensor([[1, 2]])})
        self.device = None

    def to(self, device):
        self.device = device
        return self


class FakeModel:
    name_or_path = "fake-model"
    _version = "test-version"
    config = SimpleNamespace(id2label={0: "negative", 1: "positive"})

    def __init__(self):
        self.device = None
        self.eval_called = False
        self.calls = 0

    def to(self, device):
        self.device = device
        return self

    def eval(self):
        self.eval_called = True

    def __call__(self, **inputs):
        logits = (
            torch.tensor([[0.9, 0.1]])
            if self.calls == 0
            else torch.tensor([[0.1, 0.9]])
        )
        self.calls += 1
        return SimpleNamespace(logits=logits)


def test_base_sentiment_contract_is_abstract():
    model = SentimentAPI("unused")

    for method in (model.get_name, model.get_version):
        try:
            method()
        except NotImplementedError:
            pass
        else:
            raise AssertionError("Base contract must require an implementation")


def test_pipeline_loads_model_and_tokenizer_with_configured_options():
    tokenizer = Mock()
    model = FakeModel()
    with (
        patch("models.sentiment.AutoTokenizer.from_pretrained", return_value=tokenizer) as load_tokenizer,
        patch(
            "models.sentiment.AutoModelForSequenceClassification.from_pretrained",
            return_value=model,
        ) as load_model,
    ):
        pipeline = SentimentPipelineAPI("fake-model", 128)

    assert pipeline.max_length == 128
    assert model.device == "cpu"
    assert model.eval_called is True
    load_tokenizer.assert_called_once_with("fake-model", trust_remote_code=False)
    assert load_model.call_args.kwargs["torch_dtype"] is torch.float16
    assert load_model.call_args.kwargs["trust_remote_code"] is False


def test_predict_truncates_inputs_and_preserves_order():
    batches = []

    def tokenize(text, **kwargs):
        batch = FakeBatch()
        batches.append((text, kwargs, batch))
        return batch

    model = FakeModel()
    with (
        patch("models.sentiment.AutoTokenizer.from_pretrained", return_value=tokenize),
        patch(
            "models.sentiment.AutoModelForSequenceClassification.from_pretrained",
            return_value=model,
        ),
    ):
        pipeline = SentimentPipelineAPI("fake-model", 16)
        predictions, labels = pipeline.predict(["bad", "good"])

    assert predictions == [0, 1]
    assert labels == {0: "negative", 1: "positive"}
    assert [entry[0] for entry in batches] == ["bad", "good"]
    assert all(
        entry[1] == {"return_tensors": "pt", "truncation": True, "max_length": 16}
        for entry in batches
    )
    assert all(entry[2].device == "cpu" for entry in batches)


def test_predict_runs_in_inference_mode():
    tokenizer = Mock(return_value=FakeBatch())
    model = FakeModel()
    inference_mode = Mock(wraps=torch.inference_mode)
    with (
        patch("models.sentiment.AutoTokenizer.from_pretrained", return_value=tokenizer),
        patch(
            "models.sentiment.AutoModelForSequenceClassification.from_pretrained",
            return_value=model,
        ),
        patch("models.sentiment.torch.inference_mode", inference_mode),
    ):
        SentimentPipelineAPI("fake-model", 16).predict(["example"])

    inference_mode.assert_called_once_with()


def test_clean_prediction_releases_cuda_cache():
    with patch("models.sentiment.torch.cuda.empty_cache") as empty_cache:
        SentimentAPI.clean_prediction()

    empty_cache.assert_called_once()


def test_model_factory_returns_transformers_implementation():
    assert get_model_class() is SentimentPipelineAPI
