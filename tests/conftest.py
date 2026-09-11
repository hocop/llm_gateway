from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from valkey.asyncio import Valkey

from llm_gateway.app import create_app
from llm_gateway.config import load_config
from llm_gateway.quota import QuotaStore
from llm_gateway.routing import ModelRouter
from tests.helpers import QUOTA_TIMEOUT, SECRETS_ENV, FakeProviders, Gateway, write_config

# The local Valkey server. Its database 15 belongs to tests and is emptied around every test
VALKEY_URL = "valkey://localhost:6379/15"


@pytest.fixture
async def valkey() -> AsyncIterator[Valkey]:
    client = Valkey.from_url(VALKEY_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def gateway(tmp_path: Path, valkey: Valkey) -> AsyncIterator[Gateway]:
    """The gateway app with test config, mocked providers and the test Valkey database."""
    config = load_config(write_config(tmp_path), SECRETS_ENV)
    providers = FakeProviders()
    quotas = QuotaStore(valkey, lease_ttl=5.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(providers)) as http:
        router = ModelRouter(config, quotas, http, quota_timeout=QUOTA_TIMEOUT, poll_interval=0.01)
        app = create_app(config, router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            yield Gateway(app, client, providers, valkey)
    await quotas.aclose()
