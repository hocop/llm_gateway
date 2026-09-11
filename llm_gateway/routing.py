"""Routing a client request for a virtual model to upstream providers, under the key's quotas."""

import asyncio
import enum
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
from starlette.datastructures import UploadFile
from starlette.requests import Request
from valkey.exceptions import ValkeyError

from llm_gateway.config import GatewayConfig, Provider, UpstreamRoute, VirtualKey, VirtualModel
from llm_gateway.quota import Lease, QuotaStore

logger = logging.getLogger(__name__)

# Upstream statuses meaning "this model is unavailable, try the next one", besides all 5xx
_FALLBACK_STATUSES = frozenset({404, 408, 429})


class GatewayError(Exception):
    """An error returned to the client as an OpenAI-style error response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


type FormFile = tuple[str, tuple[str | None, bytes, str | None]]


@dataclass(frozen=True)
class ClientRequest:
    """A client request to a model endpoint, kept so it can be sent to several upstreams."""

    path: str  # relative to /v1, e.g. "chat/completions"
    model: str
    json: Mapping[str, Any] | None = None
    form: Mapping[str, list[str]] = field(default_factory=dict)
    files: Sequence[FormFile] = ()

    @classmethod
    async def parse(cls, request: Request, path: str) -> "ClientRequest":
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            try:
                body = await request.json()
            except ValueError as error:
                raise GatewayError(400, "Request body is not valid JSON") from error
            if not isinstance(body, dict) or not isinstance(body.get("model"), str):
                raise GatewayError(400, "Request body must be a JSON object with a string 'model'")
            return cls(path, body["model"], json=body)

        if content_type.startswith("multipart/form-data"):
            # E.g. audio transcriptions: the model is one of the form fields
            form: dict[str, list[str]] = {}
            files: list[FormFile] = []
            for name, value in (await request.form()).multi_items():
                if isinstance(value, UploadFile):
                    files.append((name, (value.filename, await value.read(), value.content_type)))
                else:
                    form.setdefault(name, []).append(value)
            models = form.get("model", [])
            if len(models) != 1:
                raise GatewayError(400, "Form must have exactly one 'model' field")
            return cls(path, models[0], form=form, files=files)

        raise GatewayError(415, "Content type must be application/json or multipart/form-data")

    def build(self, http: httpx.AsyncClient, provider: Provider, model: str) -> httpx.Request:
        """The request to one upstream, with the virtual model replaced by the real one."""
        url = f"{provider.url.rstrip('/')}/{self.path}"
        headers = {"Authorization": f"Bearer {provider.key}"} if provider.key else None
        if self.json is not None:
            return http.build_request("POST", url, headers=headers, json={**self.json, "model": model})
        return http.build_request("POST", url, headers=headers, data={**self.form, "model": model}, files=self.files)


@dataclass
class UpstreamResponse:
    """An upstream response with a not yet consumed body, and the quota leases held until it is."""

    response: httpx.Response
    leases: list[Lease] = field(default_factory=list)

    async def aclose(self) -> None:
        for lease in self.leases:
            lease.release()
        # Closing must finish even when the client request is being cancelled
        with anyio.CancelScope(shield=True):
            await self.response.aclose()


class _Outcome(enum.Enum):
    BUSY = enum.auto()  # some route may work once quota frees up
    FAILED = enum.auto()  # every route failed


@dataclass
class _Attempt:
    """State of routing one client request."""

    key: VirtualKey
    request: ClientRequest
    failed: set[UpstreamRoute] = field(default_factory=set)
    last_error: httpx.Response | None = None  # already read, safe to return to the client


class ModelRouter:
    """Opens an upstream response for a virtual model, trying its routes in order."""

    def __init__(
        self,
        config: GatewayConfig,
        quotas: QuotaStore,
        http: httpx.AsyncClient,
        quota_timeout: float,
        poll_interval: float = 0.2,
    ) -> None:
        self._config = config
        self._quotas = quotas
        self._http = http
        self._quota_timeout = quota_timeout
        self._poll_interval = poll_interval

    async def open(self, key: VirtualKey, request: ClientRequest) -> UpstreamResponse:
        """Open the first route that has free quota and a working upstream.

        While all working routes are busy, waits for quota up to the quota timeout.
        When every route failed, returns the last upstream error response as is,
        or raises a 502 error if no upstream responded at all.
        """
        attempt = _Attempt(key, request)
        model = self._config.models[request.model]
        deadline = time.monotonic() + self._quota_timeout
        while (outcome := await self._try_model(model, attempt)) is _Outcome.BUSY:
            if time.monotonic() >= deadline:
                raise GatewayError(429, f"Quota for model {model.name!r} is exhausted, timed out waiting for it")
            await asyncio.sleep(self._poll_interval)

        if isinstance(outcome, UpstreamResponse):
            return outcome
        if attempt.last_error is not None:
            return UpstreamResponse(attempt.last_error)
        raise GatewayError(502, f"No upstream is available for model {model.name!r}")

    async def _try_model(self, model: VirtualModel, attempt: _Attempt) -> UpstreamResponse | _Outcome:
        """Try the model's routes in order, holding the key's quota for this model if it has one."""
        lease = None
        if (max_concurrency := attempt.key.max_concurrency(model.name)) is not None:
            try:
                lease = await self._quotas.try_acquire(attempt.key.name, model.name, max_concurrency)
            except ValkeyError as error:
                raise GatewayError(503, "Quota store is unavailable") from error
            if lease is None:
                return _Outcome.BUSY

        outcome = _Outcome.FAILED
        try:
            for route in model.routes:
                if isinstance(route, UpstreamRoute):
                    result = await self._try_upstream(route, attempt)
                else:
                    result = await self._try_model(self._config.models[route.name], attempt)
                if isinstance(result, UpstreamResponse):
                    if lease is not None:
                        result.leases.append(lease)
                        lease = None  # now released together with the response
                    return result
                if result is _Outcome.BUSY:
                    outcome = _Outcome.BUSY
        finally:
            if lease is not None:
                lease.release()
        return outcome

    async def _try_upstream(self, route: UpstreamRoute, attempt: _Attempt) -> UpstreamResponse | _Outcome:
        if route in attempt.failed:
            return _Outcome.FAILED
        provider = self._config.providers[route.provider]
        upstream_request = attempt.request.build(self._http, provider, route.model)
        try:
            response = await self._http.send(upstream_request, stream=True)
        except httpx.TransportError as error:
            logger.warning("Upstream %s/%s is unavailable: %r", route.provider, route.model, error)
            attempt.failed.add(route)
            return _Outcome.FAILED

        if response.status_code in _FALLBACK_STATUSES or response.status_code >= 500:
            logger.warning("Upstream %s/%s responded with %d", route.provider, route.model, response.status_code)
            attempt.failed.add(route)
            try:
                await response.aread()
                attempt.last_error = response
            except httpx.TransportError:
                pass
            finally:
                await response.aclose()
            return _Outcome.FAILED

        return UpstreamResponse(response)
