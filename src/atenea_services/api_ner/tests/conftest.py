import os
import asyncio

import httpx2 as httpx
import pytest


os.environ.setdefault("NER_SERVICE_API_KEY", "ner-test-key")
os.environ.setdefault("MAX_DATA_BY_REQUEST", "3")

from src import api  # noqa: E402


class ASGITestClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, **kwargs):
        async def send_request():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send_request())

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "ALLOWED_API_KEYS", ["ner-test-key"])
    monkeypatch.setattr(api, "NER_API", {})
    return ASGITestClient(api.app)


@pytest.fixture
def auth_headers():
    return {"ApiKey": "ner-test-key"}
