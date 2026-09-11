import asyncio
import contextlib
import itertools
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from starlette.requests import ClientDisconnect
from starlette.types import Message
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from llm_gateway.app import create_app_from_env
from tests.conftest import VALKEY_URL
from tests.helpers import (
    MESSAGES,
    SECRETS,
    SECRETS_ENV,
    Gateway,
    Handler,
    auth,
    blocked_until,
    chat,
    completion,
    eventually,
    refuse_connection,
    responding,
    unavailable,
    write_config,
)


async def test_app_from_env_starts_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environ = SECRETS_ENV | {"LLM_GATEWAY_CONFIG_DIR": str(write_config(tmp_path)), "VALKEY_URL": VALKEY_URL}
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    app = create_app_from_env()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            response = await client.get("/v1/models", headers=auth("service"))

    assert response.status_code == 200
    assert [model["id"] for model in response.json()["data"]] == ["any_model"]


async def test_models_list_shows_allowed_models_with_quotas(gateway: Gateway) -> None:
    me = (await gateway.client.get("/v1/models", headers=auth("me"))).json()
    limited = (await gateway.client.get("/v1/models", headers=auth("limited"))).json()
    service = (await gateway.client.get("/v1/models", headers=auth("service"))).json()

    assert me["object"] == "list"
    assert [model["id"] for model in me["data"]] == ["fast_model", "smart_model", "any_model", "embedding_model"]
    assert all(model["quotas"] == [] for model in me["data"])

    assert [model["id"] for model in limited["data"]] == ["fast_model", "smart_model"]
    smart_model = limited["data"][1]
    assert smart_model == {
        "id": "smart_model",
        "object": "model",
        "created": smart_model["created"],
        "owned_by": "llm_gateway",
        "description": "Smart model",
        "quotas": [{"model": "smart_model", "max_concurrency": 0.5}],
    }

    # Quotas of the models any_model routes to are listed too
    [any_model] = service["data"]
    assert any_model["id"] == "any_model"
    assert any_model["quotas"] == [
        {"model": "fast_model", "max_concurrency": 1},
        {"model": "smart_model", "max_concurrency": 1},
    ]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {SECRETS['me']}"}, {"Authorization": "Bearer"}],
)
async def test_invalid_virtual_key_is_rejected(gateway: Gateway, headers: dict[str, str]) -> None:
    models = await gateway.client.get("/v1/models", headers=headers)
    completions = await gateway.client.post("/v1/chat/completions", headers=headers, json=chat("fast_model"))

    for response in [models, completions]:
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "authentication_error"
    assert gateway.providers.requests == []


async def test_unknown_and_forbidden_models_are_rejected(gateway: Gateway) -> None:
    unknown = await gateway.client.post("/v1/chat/completions", headers=auth("me"), json=chat("gpt-5"))
    forbidden = await gateway.client.post("/v1/chat/completions", headers=auth("limited"), json=chat("any_model"))

    assert unknown.status_code == 404
    assert forbidden.status_code == 403
    assert gateway.providers.requests == []


@pytest.mark.parametrize(
    ("method", "path", "status_code"),
    [("GET", "/v1/chat/completions", 405), ("PUT", "/v1/chat/completions", 405), ("GET", "/nope", 404)],
)
async def test_unknown_endpoint_gets_openai_style_error(
    gateway: Gateway, method: str, path: str, status_code: int
) -> None:
    response = await gateway.client.request(method, path, headers=auth("me"))

    assert response.status_code == status_code
    assert set(response.json()["error"]) == {"message", "type"}


# Encoded slashes and question marks are decoded before routing, so they must not reach other upstream paths
@pytest.mark.parametrize(
    "path",
    ["/v1/..%2F..%2Fsleep", "/v1/chat%2F..%2F..%2Fmetrics", "/v1/chat/completions%3Fdebug=1", "/v1/load_lora_adapter"],
)
async def test_endpoint_outside_allowlist_is_not_forwarded(gateway: Gateway, path: str) -> None:
    response = await gateway.client.post(path, headers=auth("me"), json=chat("fast_model"))

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found_error"
    assert gateway.providers.requests == []


@pytest.mark.parametrize(
    ("content_type", "content", "status_code"),
    [
        ("application/json", b"{not json", 400),
        ("application/json", b'{"messages": []}', 400),
        ("application/json", b'["fast_model"]', 400),
        ("application/x-www-form-urlencoded", b"model=fast_model", 415),
        (
            "multipart/form-data; boundary=b",
            b'--b\r\nContent-Disposition: form-data; name="prompt"\r\n\r\nhi\r\n--b--\r\n',
            400,
        ),
    ],
)
async def test_malformed_request_is_rejected(
    gateway: Gateway, content_type: str, content: bytes, status_code: int
) -> None:
    headers = auth("me") | {"Content-Type": content_type}
    response = await gateway.client.post("/v1/chat/completions", headers=headers, content=content)

    assert response.status_code == status_code
    assert "message" in response.json()["error"]
    assert gateway.providers.requests == []


async def test_chat_completion_is_sent_to_real_model(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/v1/chat/completions", headers=auth("me"), json=chat("fast_model", temperature=0.2)
    )

    assert response.status_code == 200
    assert response.json() == completion("qwen-fast")  # usage and other fields are preserved
    [upstream] = gateway.providers.requests
    assert str(upstream.url) == "http://vllm.test/v1/chat/completions"
    assert upstream.headers["authorization"] == "Bearer vllm-secret"
    assert json.loads(upstream.content) == {"model": "qwen-fast", "messages": MESSAGES, "temperature": 0.2}


async def test_provider_without_key_gets_no_authorization(gateway: Gateway) -> None:
    response = await gateway.client.post("/v1/chat/completions", headers=auth("me"), json=chat("smart_model"))

    assert response.status_code == 200
    [upstream] = gateway.providers.requests
    assert upstream.url.host == "llama.test"
    assert "authorization" not in upstream.headers


async def test_streaming_response_is_passed_through(gateway: Gateway) -> None:
    chunks = [
        b'data: {"choices":[{"index":0,"delta":{"content":"A cat"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":2,"total_tokens":14}}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def stream(request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk

        return httpx.Response(200, headers={"Content-Type": "text/event-stream", "X-Request-Id": "42"}, content=body())

    gateway.providers.handlers["vllm.test"] = stream
    response = await gateway.client.post(
        "/v1/chat/completions", headers=auth("limited"), json=chat("fast_model", stream=True)
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["x-request-id"] == "42"
    assert response.content == b"".join(chunks)
    assert json.loads(gateway.providers.requests[0].content)["stream"] is True
    await eventually(lambda: _usage_is(gateway, "limited", "fast_model", 0))


async def test_embeddings_are_sent_to_real_model(gateway: Gateway) -> None:
    embeddings = {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "model": "bge",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }

    async def embed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=embeddings)

    gateway.providers.handlers["vllm.test"] = embed
    response = await gateway.client.post(
        "/v1/embeddings", headers=auth("me"), json={"model": "embedding_model", "input": "hello"}
    )

    assert response.status_code == 200
    assert response.json() == embeddings
    [upstream] = gateway.providers.requests
    assert str(upstream.url) == "http://vllm.test/v1/embeddings"
    assert json.loads(upstream.content) == {"model": "bge", "input": "hello"}


async def test_multipart_request_is_sent_to_real_model(gateway: Gateway) -> None:
    async def transcribe(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "hello"})

    gateway.providers.handlers["vllm.test"] = transcribe
    response = await gateway.client.post(
        "/v1/audio/transcriptions",
        headers=auth("me"),
        data={"model": "fast_model", "language": "en"},
        files={"file": ("speech.wav", b"RIFF-audio", "audio/wav")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": "hello"}
    [upstream] = gateway.providers.requests
    assert str(upstream.url) == "http://vllm.test/v1/audio/transcriptions"
    assert upstream.headers["content-type"].startswith("multipart/form-data")
    assert b'name="model"\r\n\r\nqwen-fast\r\n' in upstream.content
    assert b'name="language"\r\n\r\nen\r\n' in upstream.content
    assert b'filename="speech.wav"' in upstream.content
    assert b"RIFF-audio" in upstream.content


@pytest.mark.parametrize("failure", [refuse_connection, unavailable, *map(responding, [404, 408, 429, 500])])
async def test_failed_upstream_falls_back_to_next_model(gateway: Gateway, failure: Handler) -> None:
    gateway.providers.handlers["vllm.test"] = failure
    response = await gateway.client.post("/v1/chat/completions", headers=auth("service"), json=chat("any_model"))

    assert response.status_code == 200
    assert response.json()["model"] == "qwen-smart"
    assert gateway.providers.hosts == ["vllm.test", "llama.test"]
    # Quota taken for the failed model is given back too
    await eventually(lambda: _usage_is(gateway, "service", "fast_model", 0))
    await eventually(lambda: _usage_is(gateway, "service", "smart_model", 0))


async def test_client_error_is_returned_without_fallback(gateway: Gateway) -> None:
    async def bad_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Prompt is too long"}})

    gateway.providers.handlers["vllm.test"] = bad_request
    response = await gateway.client.post("/v1/chat/completions", headers=auth("service"), json=chat("any_model"))

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "Prompt is too long"}}
    assert gateway.providers.hosts == ["vllm.test"]


async def test_last_upstream_error_is_returned_when_all_fail(gateway: Gateway) -> None:
    gateway.providers.handlers["vllm.test"] = unavailable
    gateway.providers.handlers["llama.test"] = unavailable
    response = await gateway.client.post("/v1/chat/completions", headers=auth("me"), json=chat("any_model"))

    assert response.status_code == 503
    assert response.json() == {"error": "Loading model"}
    assert gateway.providers.hosts == ["vllm.test", "llama.test"]


async def test_bad_gateway_when_no_upstream_responds(gateway: Gateway) -> None:
    gateway.providers.handlers["vllm.test"] = refuse_connection
    gateway.providers.handlers["llama.test"] = refuse_connection
    response = await gateway.client.post("/v1/chat/completions", headers=auth("me"), json=chat("any_model"))

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "api_error"


async def test_quota_limits_concurrent_requests(gateway: Gateway) -> None:
    finish = asyncio.Event()
    gateway.providers.handlers["vllm.test"] = blocked_until(finish)
    request = chat("fast_model")  # limited to 1 concurrent request

    first = asyncio.create_task(gateway.client.post("/v1/chat/completions", headers=auth("limited"), json=request))
    await eventually(lambda: _request_count_is(gateway, 1))
    second = asyncio.create_task(gateway.client.post("/v1/chat/completions", headers=auth("limited"), json=request))
    await asyncio.sleep(0.1)
    assert len(gateway.providers.requests) == 1  # the second request waits for quota

    finish.set()
    assert (await first).status_code == 200
    assert (await second).status_code == 200
    assert len(gateway.providers.requests) == 2


async def test_unlimited_key_is_not_queued(gateway: Gateway) -> None:
    finish = asyncio.Event()
    gateway.providers.handlers["vllm.test"] = blocked_until(finish)

    requests = [
        asyncio.create_task(gateway.client.post("/v1/chat/completions", headers=auth("me"), json=chat("fast_model")))
        for _ in range(3)
    ]
    await eventually(lambda: _request_count_is(gateway, 3))
    finish.set()

    assert [(await request).status_code for request in requests] == [200, 200, 200]
    assert await gateway.valkey.keys("*") == []


async def test_quota_wait_times_out(gateway: Gateway) -> None:
    finish = asyncio.Event()
    gateway.providers.handlers["vllm.test"] = blocked_until(finish)

    first = asyncio.create_task(
        gateway.client.post("/v1/chat/completions", headers=auth("limited"), json=chat("fast_model"))
    )
    await eventually(lambda: _request_count_is(gateway, 1))
    second = await gateway.client.post("/v1/chat/completions", headers=auth("limited"), json=chat("fast_model"))

    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limit_error"
    finish.set()
    assert (await first).status_code == 200


async def test_busy_model_falls_through_to_next_model(gateway: Gateway) -> None:
    finish = asyncio.Event()
    gateway.providers.handlers["vllm.test"] = blocked_until(finish)

    first = asyncio.create_task(
        gateway.client.post("/v1/chat/completions", headers=auth("service"), json=chat("any_model"))
    )
    await eventually(lambda: _request_count_is(gateway, 1))
    # fast_model quota of the key is taken, so smart_model serves the next request
    second = await gateway.client.post("/v1/chat/completions", headers=auth("service"), json=chat("any_model"))

    assert second.status_code == 200
    assert second.json()["model"] == "qwen-smart"
    finish.set()
    assert (await first).json()["model"] == "qwen-fast"


async def test_fractional_quota_holds_capacity_for_residual_time(gateway: Gateway) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=completion("qwen-smart"))

    gateway.providers.handlers["llama.test"] = slow
    started = time.monotonic()
    response = await gateway.client.post("/v1/chat/completions", headers=auth("limited"), json=chat("smart_model"))

    assert response.status_code == 200
    # smart_model quota is 0.5: the capacity stays taken for as long as the request ran
    assert await gateway.quota_usage("limited", "smart_model") == 0.5
    await eventually(lambda: _usage_is(gateway, "limited", "smart_model", 0))
    assert time.monotonic() - started >= 0.4


async def test_quota_store_outage_fails_only_limited_requests(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args: object) -> None:
        raise ValkeyConnectionError("Connection refused")

    monkeypatch.setattr(gateway.valkey, "eval", fail)
    limited = await gateway.client.post("/v1/chat/completions", headers=auth("limited"), json=chat("fast_model"))
    unlimited = await gateway.client.post("/v1/chat/completions", headers=auth("me"), json=chat("fast_model"))

    assert limited.status_code == 503
    assert unlimited.status_code == 200


# Servers report a disconnect by receive() before ASGI spec 2.4 (uvicorn), and by send() raising OSError since
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_client_disconnect_stops_stream_and_releases_quota(gateway: Gateway, spec_version: str) -> None:
    produced: list[int] = []

    async def endless_stream(request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            for index in itertools.count():
                produced.append(index)
                yield b'data: {"choices":[]}\n\n'
                await asyncio.sleep(0.01)

        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body())

    gateway.providers.handlers["vllm.test"] = endless_stream
    # Drive the ASGI app directly, as a server would: the client leaves after the first chunk
    sent: list[Message] = []
    body = json.dumps(chat("fast_model", stream=True)).encode()
    incoming: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]

    def client_left() -> bool:
        return any(message.get("body") for message in sent)

    async def receive() -> Message:
        if incoming:
            return incoming.pop(0)
        while not client_left():
            await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if spec_version == "2.4" and client_left():
            raise OSError("Connection reset by peer")
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", f"Bearer {SECRETS['limited']}".encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("gateway", 80),
    }
    async with asyncio.timeout(2):
        with contextlib.suppress(ClientDisconnect):
            await gateway.app(scope, receive, send)

    assert sent[0]["status"] == 200
    assert len(produced) < 5
    await eventually(lambda: _usage_is(gateway, "limited", "fast_model", 0))


async def _usage_is(gateway: Gateway, key: str, model: str, usage: float) -> bool:
    return await gateway.quota_usage(key, model) == usage


async def _request_count_is(gateway: Gateway, count: int) -> bool:
    return len(gateway.providers.requests) == count
