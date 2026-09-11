"""FastAPI application exposing the OpenAI-compatible gateway API."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
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
    415: "invalid_request_error",
    429: "rate_limit_error",
}


class _ProxiedResponse(StreamingResponse):
    """Streams an upstream response to the client, releasing its quota leases afterwards."""

    def __init__(self, upstream: UpstreamResponse, request: Request) -> None:
        response = upstream.response
        headers = {name: value for name, value in response.headers.items() if name not in _SKIPPED_RESPONSE_HEADERS}
        super().__init__(self._body(response, request), status_code=response.status_code, headers=headers)
        self._upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Cleanup lives here rather than in the body generator, which may never start
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._upstream.aclose()

    @staticmethod
    async def _body(response: httpx.Response, request: Request) -> AsyncIterator[bytes]:
        async for chunk in response.aiter_bytes():
            yield chunk
            # Stop reading for a gone client, so the quota frees up and the upstream aborts generation
            if await request.is_disconnected():
                break


def create_app(
    config: GatewayConfig,
    router: ModelRouter,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="LLM Gateway", lifespan=lifespan)
    started_at = int(time.time())

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, error: GatewayError) -> JSONResponse:
        error_type = _ERROR_TYPES.get(error.status_code, "api_error")
        return JSONResponse({"error": {"message": error.message, "type": error_type}}, error.status_code)

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
        """Forwards any model endpoint: chat completions, completions, embeddings, audio and others."""
        client_request = await ClientRequest.parse(request, path)
        if client_request.model not in config.models:
            raise GatewayError(404, f"Model {client_request.model!r} does not exist")
        if not key.allows(client_request.model):
            raise GatewayError(403, f"Virtual key is not allowed to use model {client_request.model!r}")
        upstream = await router.open(key, client_request)
        return _ProxiedResponse(upstream, request)

    return app


def create_app_from_env() -> FastAPI:
    """Uvicorn app factory: settings and secrets come from environment variables."""
    settings = Settings.from_env()
    config = load_config(settings.config_dir)
    valkey = Valkey.from_url(settings.valkey_url, decode_responses=True)
    http = httpx.AsyncClient(timeout=httpx.Timeout(settings.upstream_timeout, connect=10.0))
    quotas = QuotaStore(valkey)
    router = ModelRouter(config, quotas, http, quota_timeout=settings.quota_timeout)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await quotas.aclose()
        await http.aclose()
        await valkey.aclose()

    return create_app(config, router, lifespan)
