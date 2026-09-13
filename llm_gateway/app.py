"""FastAPI application exposing the OpenAI-compatible gateway API."""

import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send
from valkey.asyncio import Valkey

from llm_gateway.config import GatewayConfig, VirtualKey, load_config
from llm_gateway.quota import QuotaStore
from llm_gateway.routing import ClientRequest, GatewayError, ModelRouter, UpstreamResponse
from llm_gateway.settings import Settings

# Connection and body framing headers are set by our own server
_SKIPPED_RESPONSE_HEADERS = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "date", "server"}
)

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    405: "invalid_request_error",
    415: "invalid_request_error",
    429: "rate_limit_error",
}

# Model endpoints of llama.cpp and vLLM. The path is appended to provider URLs, so anything else could reach
# their admin endpoints: "../sleep" or an encoded "?" survive routing
_MODEL_ENDPOINTS = frozenset(
    {
        "chat/completions",
        "completions",
        "embeddings",
        "responses",
        "messages",
        "rerank",
        "score",
        "audio/transcriptions",
        "audio/translations",
        "audio/speech",
    }
)


class _Labeller:
    """Replaces the real model name in JSON bodies, telling which route served the request."""

    def __init__(self, model: str, last_virtual_model: str, provider: str) -> None:
        self._model = model
        # Only the first labelled body carries them, so later frames of a stream stay small. They are nested in
        # `extra_fields`, as Bifrost does, so that they don't contend with fields of the response schema
        self._extra_fields: dict[str, str] | None = {"last_virtual_model": last_virtual_model, "provider": provider}

    def apply(self, body: bytes) -> bytes:
        """The body with the model name replaced, or the body as it is when it carries none."""
        try:
            payload = json.loads(body)
        except ValueError:
            return body
        if not isinstance(payload, dict) or "model" not in payload:
            return body
        payload["model"] = self._model
        if self._extra_fields is not None:
            payload["extra_fields"] = self._extra_fields
            self._extra_fields = None
        return json.dumps(payload, ensure_ascii=False).encode()


async def _labelled_json(response: httpx.Response, labeller: _Labeller) -> AsyncIterator[bytes]:
    yield labeller.apply(await response.aread())


async def _labelled_events(response: httpx.Response, labeller: _Labeller) -> AsyncIterator[bytes]:
    """Relabels a server-sent event stream line by line, as chunks may split a frame anywhere."""
    buffer = b""
    async for chunk in response.aiter_bytes():
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            yield _labelled_line(line, labeller) + b"\n"
    if buffer:
        yield _labelled_line(buffer, labeller)


def _labelled_line(line: bytes, labeller: _Labeller) -> bytes:
    """A `data:` line with its JSON payload relabelled. Other lines and `[DONE]` are left alone."""
    prefix, colon, payload = line.partition(b":")
    data = payload.strip()
    if prefix != b"data" or not colon or not data or data == b"[DONE]":
        return line
    return b"data: " + labeller.apply(data)


def _relabelled(upstream: UpstreamResponse, model: str) -> AsyncIterator[bytes]:
    """The upstream body, with the real model name replaced by the virtual model the client asked for."""
    response = upstream.response
    if not response.is_success or not upstream.virtual_model:
        return response.aiter_bytes()  # upstream errors are returned as they are
    labeller = _Labeller(model, upstream.virtual_model, upstream.provider)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return _labelled_json(response, labeller)
    if content_type.startswith("text/event-stream"):
        return _labelled_events(response, labeller)
    return response.aiter_bytes()  # audio and other binary bodies carry no model name


class _ProxiedResponse(StreamingResponse):
    """Streams an upstream response to the client, relabelled, releasing its quota leases afterwards."""

    def __init__(self, upstream: UpstreamResponse, model: str) -> None:
        response = upstream.response
        headers = {name: value for name, value in response.headers.items() if name not in _SKIPPED_RESPONSE_HEADERS}
        super().__init__(_relabelled(upstream, model), status_code=response.status_code, headers=headers)
        self._upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Cleanup lives here rather than in the body generator, which may never start.
        # Starlette stops streaming when the client disconnects, so the upstream aborts generation too.
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._upstream.aclose()


def _error_response(status_code: int, message: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    error_type = _ERROR_TYPES.get(status_code, "api_error")
    return JSONResponse({"error": {"message": message, "type": error_type}}, status_code, headers)


def create_app(
    config: GatewayConfig,
    router: ModelRouter,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="LLM Gateway", lifespan=lifespan)
    started_at = int(time.time())

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, error: GatewayError) -> JSONResponse:
        return _error_response(error.status_code, error.message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        # Unknown paths and methods, so that they get OpenAI-style errors too
        return _error_response(error.status_code, error.detail, error.headers)

    async def authenticate(authorization: Annotated[str, Header()] = "") -> VirtualKey:
        scheme, _, secret = authorization.partition(" ")
        key = config.find_key(secret) if scheme.lower() == "bearer" and secret else None
        if key is None:
            raise GatewayError(401, "Invalid virtual key")
        return key

    AuthorizedKey = Annotated[VirtualKey, Depends(authenticate)]

    @app.get("/v1/models")
    async def list_models(key: AuthorizedKey) -> dict[str, Any]:
        """Virtual models available to the key, with the key's quotas for them and the models they route to."""
        models = []
        for model in config.models.values():
            if not key.allows(model.name):
                continue
            quotas = [
                {"model": name, "max_concurrency": max_concurrency}
                for name in config.referenced_models(model.name)
                if (max_concurrency := key.max_concurrency(name)) is not None
            ]
            models.append(
                {
                    "id": model.name,
                    "object": "model",
                    "created": started_at,
                    "owned_by": "llm_gateway",
                    "description": model.description,
                    "quotas": quotas,
                }
            )
        return {"object": "list", "data": models}

    @app.post("/v1/{path:path}")
    async def proxy(path: str, request: Request, key: AuthorizedKey) -> StreamingResponse:
        """Forwards a model endpoint: chat completions, completions, embeddings, audio and others."""
        if path not in _MODEL_ENDPOINTS:
            raise GatewayError(404, f"Endpoint /v1/{path} does not exist")
        client_request = await ClientRequest.parse(request, path)
        if client_request.model not in config.models:
            raise GatewayError(404, f"Model {client_request.model!r} does not exist")
        if not key.allows(client_request.model):
            raise GatewayError(403, f"Virtual key is not allowed to use model {client_request.model!r}")
        upstream = await router.open(key, client_request)
        return _ProxiedResponse(upstream, client_request.model)

    return app


def create_app_from_env() -> FastAPI:
    """Uvicorn app factory: settings and secrets come from environment variables."""
    settings = Settings.from_env()
    config = load_config(settings.config_dir)
    valkey = Valkey.from_url(settings.valkey_url, decode_responses=True)
    # No cap on connections: quotas limit concurrency, and a request must not wait for the pool while holding a lease
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.upstream_timeout, connect=10.0),
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=20),
    )
    quotas = QuotaStore(valkey)
    router = ModelRouter(config, quotas, http, quota_timeout=settings.quota_timeout)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await quotas.aclose()
        await http.aclose()
        await valkey.aclose()

    return create_app(config, router, lifespan)
