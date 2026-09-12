"""Test helpers: config files, mocked upstream providers and an in-process gateway."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI
from valkey.asyncio import Valkey

PROVIDERS_TOML = """
[[providers]]
name = "llama_cpp"
url = "http://llama.test/v1"

[[providers]]
name = "vllm"
url = "http://vllm.test/v1"
key = "vllm-secret"
"""

VIRTUAL_MODELS_TOML = """
[[virtual_models]]
name = "fast_model"
model = "vllm/qwen-fast"
description = "Fast model"

[[virtual_models]]
name = "smart_model"
model = "llama_cpp/qwen-smart"
description = "Smart model"

[[virtual_models]]
name = "first_available"
model = ["/fast_model", "/smart_model"]
description = "Fast model, or smart model when fast is unavailable"

[[virtual_models]]
name = "embedding_model"
model = "vllm/bge"
"""

VIRTUAL_KEYS_TOML = """
[[virtual_keys]]
name = "me"
models = "*"

[[virtual_keys]]
name = "limited"
models = ["fast_model", "smart_model"]
quotas = [
    { model = "fast_model", max_concurrency = 1 },
    { model = "smart_model", max_concurrency = 0.5 },
]

[[virtual_keys]]
name = "service"
models = ["first_available"]
quotas = [
    { model = "fast_model", max_concurrency = 1 },
    { model = "smart_model", max_concurrency = 1 },
]
"""

SECRETS = {"me": "secret-me", "limited": "secret-limited", "service": "secret-service"}
SECRETS_ENV = {f"LLM_KEY_{name.upper()}": secret for name, secret in SECRETS.items()}

QUOTA_TIMEOUT = 0.5

MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "What is in the image?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        ],
    }
]


def write_config(
    config_dir: Path,
    providers: str = PROVIDERS_TOML,
    virtual_models: str = VIRTUAL_MODELS_TOML,
    virtual_keys: str = VIRTUAL_KEYS_TOML,
) -> Path:
    (config_dir / "providers.toml").write_text(providers)
    (config_dir / "virtual_models.toml").write_text(virtual_models)
    (config_dir / "virtual_keys.toml").write_text(virtual_keys)
    return config_dir


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {SECRETS[key]}"}


def chat(model: str, **fields: object) -> dict[str, object]:
    """A multimodal chat completion request."""
    return {"model": model, "messages": MESSAGES, **fields}


def completion(model: str) -> dict[str, object]:
    """A chat completion response, with usage, from a real model."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "A cat"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
    }


type Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class FakeProviders:
    """Mocked OpenAI-compatible providers. They answer chat completions unless a test sets a handler."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handlers: dict[str, Handler] = {}  # by provider host

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if handler := self.handlers.get(request.url.host):
            return await handler(request)
        return httpx.Response(200, json=completion(json.loads(request.content)["model"]))

    @property
    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


async def refuse_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Connection refused", request=request)


async def unavailable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": "Loading model"})


def responding(status_code: int) -> Handler:
    """A provider that answers every request with an error status."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "Failed"})

    return handler


def blocked_until(event: asyncio.Event) -> Handler:
    """A provider that answers only once the event is set, to keep requests running."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await event.wait()
        return httpx.Response(200, json=completion(json.loads(request.content)["model"]))

    return handler


@dataclass
class Gateway:
    app: FastAPI
    client: httpx.AsyncClient
    providers: FakeProviders
    valkey: Valkey

    async def quota_usage(self, key: str, model: str) -> float:
        """Capacity taken by the current leases of a quota."""
        members = await self.valkey.zrange(f"llm_gateway:quota:{key}:{model}", 0, -1)
        return sum(int(member.rpartition("|")[2]) for member in members) / 100

    async def queue_length(self, key: str, model: str) -> int:
        """Number of tickets in the queue of a quota."""
        return await self.valkey.zcard(f"llm_gateway:queue:{key}:{model}")


async def eventually(condition: Callable[[], Awaitable[bool]], timeout: float = 2.0) -> None:
    """Wait until the condition holds, failing after the timeout."""
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0.01)
