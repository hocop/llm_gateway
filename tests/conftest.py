from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fakeredis import FakeAsyncValkey, FakeServer

from llm_gateway.app import create_app
from llm_gateway.config import load_config
from llm_gateway.quota import QuotaStore
from llm_gateway.routing import ModelRouter
from tests.helpers import QUOTA_TIMEOUT, SECRETS_ENV, FakeProviders, Gateway, write_config


@pytest.fixture
async def valkey() -> AsyncIterator[FakeAsyncValkey]:
    # A separate server per test, fake clients share data by default
    client = FakeAsyncValkey(server=FakeServer(), decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def gateway(tmp_path: Path, valkey: FakeAsyncValkey) -> AsyncIterator[Gateway]:
    """The gateway app with test config, mocked providers and an in-memory Valkey."""
    config = load_config(write_config(tmp_path), SECRETS_ENV)
    providers = FakeProviders()
    quotas = QuotaStore(valkey, lease_ttl=5.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(providers)) as http:
        router = ModelRouter(config, quotas, http, quota_timeout=QUOTA_TIMEOUT, poll_interval=0.01)
        app = create_app(config, router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            yield Gateway(app, client, providers, valkey)
    await quotas.aclose()
