# 000 Initial design

Implements `docs/proposals/000_initial_design.md`.

## Why

We need one OpenAI-compatible endpoint in front of several llama.cpp and vLLM servers that:
- hides real model names behind renameable virtual models, with fallback between them when a model is
  unavailable;
- hands out virtual keys configured in TOML, with secrets in environment variables;
- limits each key by concurrency, including fractional values like `0.5`, instead of RPM/TPM. For LLM
  servers the number of requests running at once is the real limited resource.

Robustness matters most. All dynamic state must live in Valkey, so replicas can be added or redeployed freely.

## How

- **Config** is three TOML files validated by pydantic models and cross-checked at startup (references,
  cycles, quotas, secrets). A virtual model's `model` is parsed into routes: `provider/model` is an upstream,
  `/name` is a reference to another virtual model.
- **Routing** walks the requested virtual model's routes in order, recursing into references. Before entering
  a virtual model, it takes a lease of the key's quota for that model, if there is one. A route is skipped when
  its quota is busy or its upstream fails (connection error, timeout, 404, 408, 429, 5xx). If some routes were
  only busy, the walk is repeated until the quota timeout (then 429). If all failed, the last upstream error is
  returned, or 502.
- **Quotas** follow the proposal's "store each request with its budget and a TTL" idea. Each quota is a Valkey
  sorted set of leases (`<id>|<budget>` scored by expiry). One Lua script atomically sweeps expired leases,
  sums the rest and takes `min(1, free)` if at least `0.1` is free. A background task refreshes the lease while
  the request runs and for the residual time `elapsed * (1 - budget) / budget` after it, then removes it.
- **Proxying** buffers the client request (JSON or multipart), rewrites `model` for each upstream attempt and
  streams the upstream response back byte for byte. Leases are released in the response's ASGI `__call__`
  `finally`, after the body is sent or the client disconnects.

## What other options were considered

- **Worker queues per key** (the proposal's mental model): rejected, as the proposal itself notes. Idle workers,
  no way to express unlimited capacity, and queues in RAM don't survive redeploys or scale across replicas.
- **A running counter with increment/decrement under a mutex** (the proposal's pseudo-code): rejected. A crash
  between acquire and release leaks capacity forever, and float additions drift. Leases recomputed from scratch
  on every acquire fix both, and a Lua script is atomic in Valkey, so no separate lock is needed.
- **Client clocks for lease expiry**: rejected in favor of Valkey's `TIME`, so clock skew between replicas
  doesn't matter.
- **`EVALSHA` with cached scripts**: rejected for plain `EVAL`. A Valkey restart flushes the script cache, and
  fakeredis doesn't handle valkey-py's `NoScriptError` fallback. The scripts are tiny and traffic is low.
- **Waiting on a busy quota instead of falling through to the next model**: rejected by the user. Falling
  through matches "route to smart if fast is not available" and uses all of a key's quotas.
- **Rewriting the `model` field in responses back to the virtual name**: rejected by the user. It would need
  parsing every JSON body and SSE chunk, while passthrough keeps all fields exactly as upstream sent them.
- **Pub/sub signals to wake requests waiting for quota**: deferred. Polling every 0.2 s is simple and enough at
  1-20 RPS. The cost is that waiting requests aren't served in arrival order.
- **Sleeping the residual time before returning the response**: rejected. The client would be delayed for
  nothing, so the residual hold runs in the background instead.
- **Releasing leases in the body generator's `finally` or in a `BackgroundTask`**: rejected. Starlette skips
  background tasks on client disconnect (ASGI spec 2.4), and a generator that never started never runs its
  `finally`. Either way a lease would be refreshed forever.
- **Allowing requests without limits while Valkey is down (fail-open)**: rejected. Quotas are the main goal,
  so requests needing a quota get 503, while keys and models without quotas keep working.
- **A real Valkey in tests**: not available locally (no valkey-server or Docker), so tests use fakeredis with
  Lua support.

## What was implemented

- `llm_gateway/settings.py`: environment settings (`LLM_GATEWAY_CONFIG_DIR`, `VALKEY_URL`,
  `LLM_GATEWAY_QUOTA_TIMEOUT`, `LLM_GATEWAY_UPSTREAM_TIMEOUT`).
- `llm_gateway/config.py`: TOML loading, startup validation, `LLM_KEY_<NAME>` secrets, wildcard model access.
- `llm_gateway/quota.py`: `QuotaStore` and `Lease` on Valkey sorted sets with Lua scripts, heartbeats and
  residual hold.
- `llm_gateway/routing.py`: `ClientRequest` (JSON and multipart), `ModelRouter` with fallback, quota
  fall-through and waiting.
- `llm_gateway/app.py`: FastAPI app with bearer auth, `GET /v1/models` with descriptions and quotas,
  `POST /v1/{path}` proxy, streaming responses, OpenAI-style errors; `main.py` runs uvicorn.
- `examples/config/`: valid TOML versions of the proposal's examples.
- `tests/`: 55 tests with mocked providers and fakeredis, covering config validation, quota leases, routing,
  fallback, streaming, multipart, client disconnect and a Valkey outage.
- `docs/wiki/configuration.md` and `docs/wiki/architecture.md`.
