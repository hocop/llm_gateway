# 003 Review by qwen flash

## Why

`docs/proposals/001_review_by_qwen_flash.md` is a code review written by another model, meant as suggestions rather
than requirements. Its findings were checked against the code and the installed Starlette 1.6, uvicorn 0.52 and
httpx 0.28 before acting on them. Several were real gaps: a request could wait for a pooled connection while
holding a quota lease, typos in provider URLs and key model patterns passed startup validation, unknown paths got
non-OpenAI errors, and some failure paths had no tests. Some others were based on wrong claims.

(The proposal has index 001, but change note 001 was already taken, so this note uses the next unused index.)

## How

- **Client disconnect (A1).** The review said `request.is_disconnected()` never fires under uvicorn. That is
  wrong: uvicorn's `receive()` returns `http.disconnect` at once for a gone client. But uvicorn declares ASGI spec
  2.3, so Starlette's `StreamingResponse` already runs `listen_for_disconnect` and cancels the stream, and for spec
  2.4 servers `send()` raises `OSError`. The polling after each chunk was redundant and was removed; the body is
  `response.aiter_bytes()` directly, and cleanup stays in `_ProxiedResponse.__call__`.
- **Connection pool (A2).** httpx caps a client at 100 connections by default, and requests wait for the pool
  after taking their quota lease. The cap is removed (`max_connections=None`, keep-alive stays at 20), as quotas
  are what limits concurrency.
- **Provider URLs (A3).** `Provider.url` must match `^https?://[^/]+`. Before, `localhost:8000/v1` loaded fine
  and made every request a 502.
- **Error shape (A6).** A handler for Starlette's `HTTPException` returns `{"error": {"message", "type"}}` for
  unknown paths (404) and methods (405), sharing `_error_response` with `GatewayError`. 405 maps to
  `invalid_request_error`.
- **Key model patterns (C).** The review noted that an unbalanced pattern like `foo[` matches nothing. More
  generally, any wildcard pattern was exempt from validation, so `smrt_*` passed. Now every pattern, wildcard or
  not, must match at least one virtual model.
- **Tests (B4).** Fallback on 404, 408, 429 and 500; the disconnect test for both ASGI spec 2.3 and 2.4; a lease
  keeps being refreshed through a Valkey outage and expires when its removal fails; `create_app_from_env` starts
  and shuts down through its lifespan.
- **Small fixes.** The `pyproject.toml` description, and `architecture.md` no longer says bodies pass "byte for
  byte": `aiter_bytes` decompresses them, which is why `Content-Encoding` is dropped.

## What other options were considered

- **Testing disconnects through a real uvicorn server in `tests/`**: rejected. The in-process test drives the ASGI
  app the way both spec versions do. A one-off script ran a real uvicorn gateway against a streaming upstream
  instead: after the client left, the upstream produced no more chunks and the lease was removed.
- **Sizing `httpx.Limits` above the expected load**: rejected. Any number is a second, hidden concurrency limit.
- **Streaming multipart uploads from a temp file, or capping their size (A4)**: rejected. Keys are authenticated,
  load is low, and an upload is held in memory once, not once per route as the review said.
- **`Retry-After` on 429 (A5)**: rejected. Leases of running requests are refreshed, so their expiry doesn't say
  when capacity frees up.
- **Forwarding query strings and duplicate response headers (A7)**: rejected. vLLM and llama.cpp need neither,
  httpx joins duplicates with commas (valid for all headers but `Set-Cookie`), and Starlette's `Response` doesn't
  accept a header list as the review claimed.
- **Jitter, `retry_after` from the acquire script, or pub/sub for waiting (B1)**: rejected. At 1-20 RPS polling
  costs nothing, and none of these makes waiting fair.
- **Settings for lease TTL and poll interval, validation of settings (B3)**: rejected. Not needed, and a bad value
  already fails at startup with a traceback naming the variable.
- **fakeredis lane for tests (B4)**: rejected earlier in `001_real_valkey_in_tests.md`.
- **Precomputed reference closure, a key index by secret, an `_Outcome` refactor, a shared `created` time (C)**:
  rejected as irrelevant at this scale or a matter of taste.
- **Health endpoints, metrics (B2), Dockerfile, CI, lint config, committing `uv.lock`, README (B5)**: deferred as
  new features.

## What was implemented

- `llm_gateway/app.py`: disconnect polling removed, OpenAI-style errors for Starlette's `HTTPException`, no
  connection cap in `create_app_from_env`.
- `llm_gateway/config.py`: provider URL pattern, key model patterns must match a virtual model.
- `tests/test_app.py`, `tests/test_config.py`, `tests/test_quota.py`, `tests/helpers.py` (`responding` handler):
  12 new test cases, 67 in total, passing 5 runs in a row; `uv run ty check` passes.
- `docs/wiki/architecture.md`, `docs/wiki/configuration.md`, `pyproject.toml` updated.
