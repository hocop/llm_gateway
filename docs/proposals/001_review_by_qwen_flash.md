The following is written by qwen. Consider it propositions, not hard guidelines

---

Reviewed all 5 modules, tests, docs and configs (55 tests pass, ty clean). Findings, worst first. Everything below
is verified against the code, not inferred.

A. Correctness / robustness bugs

A1. Dead disconnect detection — the "free quota when client leaves" path never runs in production. app.py:51-55
polls request.is_disconnected() after each chunk. In Starlette 1.6 that method is a no-op unless a non-empty-body
http.request message is sitting in receive — and under uvicorn (ASGI spec ≥ 2.4, see starlette/responses.py
__call__) a disconnect arrives as http.disconnect on a cancelled task, not through receive. So in prod the loop
exits via task cancellation (which does close the upstream — behaviour is roughly fine), while the explicit code and
test_client_disconnect_stops_stream_and_releases_quota (test_app.py, hand-rolled receive that fakes the message)
only exercise the test harness. Drop the polling and rely on cancellation, or add a test through a real uvicorn
server. Docs currently promise behaviour that the code doesn't deliver.

A2. Quota lease is held while blocked on the httpx connection pool. app.py:124 builds the client with no limits, so
httpx's default 100 connections apply. _try_upstream (routing.py:191-195) sends after the lease is taken, so when
the pool is saturated, queued requests hold quota they aren't using and other requests get 429. Set explicit
httpx.Limits sized above max expected in-flight work, or document the interaction.

A3. Provider URLs are never validated at startup — a typo degrades to silent 502s. config.py:65 url: str.
"localhost:8000/v1" makes httpx raise UnsupportedProtocol, which subclasses TransportError (verified) → caught at
routing.py:196, route marked failed, every request 502. Startup validation is the project's stated strength; add a
scheme/host check in Provider.

A4. Multipart bodies are fully buffered in RAM, once per route attempt. routing.py:68-71 await value.read() and the
bytes are re-sent to every route (build, routing.py:85). With audio/transcriptions uploads and no body-size limit,
this is a memory DoS and wasteful for a 3-route fallback. At minimum cap upload size; ideally stream from a spooled
temp file.

A5. 429 carries no Retry-After and no error code. routing.py:144 + _ERROR_TYPES. OpenAI clients back off on their
own; without Retry-After they back off arbitrarily while you know exactly when capacity frees (nearest lease
expiry). This is the cheapest real win for the queueing story.

A6. Starlette's own errors leak a non-OpenAI shape, and some verbs get wrong codes. Verified: GET /v1/nope, PUT
/v1/chat/completions, GET /v1/models/x → 405 {"detail":"Method Not Allowed"}; POST /v1 → 307. OpenAI SDKs expect
{"error": {...}}. Add handlers for HTTPException/404/405/500, and avoid the redirect.

A7. Query strings are dropped upstream; duplicate response headers are collapsed. Verified POST
/v1/chat/completions?api-version=… reaches the upstream with no query (Azure-style endpoints need it). And app.py:39
 builds a dict from response.headers.items(), so two set-cookie (or link) headers become one. Keep the ordered pair
list (Response accepts a list).

B. Design gaps worth deciding on deliberately

B1. Polling is the weak point of the quota design. Fixed 200 ms interval, no jitter (routing.py:124,145), and each
poll is a Lua EVAL that ZRANGEs and re-sums every lease — O(waiters × leases) per second, worst exactly when the
system is hot. Waiting is also unordered (docs admit it) so a busy key can starve. Options, in ascending effort:
jitter the interval; return the computed retry_after from the acquire script and sleep exactly that (pairs with A5);
or have the release path PUBLISH on the quota channel and wait with a subscription. Also consider storing budgets as
integer hundredths to delete the MIN_BUDGET - 1e-9 fudge (quota.py:69).

B2. Observability is missing entirely, given "robustness is the top priority". No /healthz//ready (neither Valkey
reachability nor config load is exposed), no metrics (per-model in-flight vs quota, fallback counts,
429-with-timeout counts, lease expiry sweeps), no logging of why a request fell through, no request-id propagation
to upstreams. At 1-20 RPS this is cheap and it's the thing you'll want first at 3 a.m.

B3. The two most operationally sensitive numbers aren't configurable. lease_ttl=30.0 (quota.py:58) and
poll_interval=0.2 (routing.py:124) are defaults in signatures; settings.py exposes only two timeouts. Conversely
Settings.from_env does bare float(...) — a bad LLM_GATEWAY_QUOTA_TIMEOUT gives a raw traceback at startup, and
nothing rejects 0/negative.

B4. Tests are no longer hermetic after the fakeredis → real Valkey switch. conftest.py:14 hardcodes
valkey://localhost:6379/15: uv run pytest now fails on a clean machine, and CI would need a service container. If
the reason was Lua fidelity (fair — EVAL/TIME semantics are the core of the design), consider keeping a fakeredis
lane for quota logic plus the real-server lane for the scripts, gated by a marker or env var. Also untested: 408
fallback (_FALLBACK_STATUSES), Settings.from_env, create_app_from_env/lifespan cleanup, Valkey failure on the
release/refresh path (quota.py:118-121,131-135 only logs).

B5. Repo hygiene. No CI, no ruff/lint config, no Dockerfile; uv.lock is gitignored (kills reproducible builds —
usually the opposite convention); README.md is empty and pyproject.toml:3 still says "Add your description here".

C. Nits (fix opportunistically, don't churn)

- config.py:117-131 — _references is BFS-per-model (_check_routes is O(V·E)) and referenced_models re-walks on every
  /v1/models call. Precompute the closure once at load and store it in GatewayConfig.
- routing.py:118-152 — UpstreamResponse | _Outcome where BUSY/FAILED are the "no payload" cases reads awkwardly; a
  tiny result dataclass (or raising for BUSY) would make open shorter to follow.
- quota.py:100-108 — residual-hold maths uses the local monotonic clock while expiry uses Valkey server time;
  correct, but deserves the one-line comment that explains why.
- app.py:64 — started_at per app instance means N uvicorn workers report N different created values in /v1/models.
- config.py:176-180 — an unbalanced wildcard pattern (foo[) passes validation and silently matches nothing.
- config.py:106-111 — find_key scans all secrets per request; irrelevant at 20 RPS, worth a dict index only if key
  count grows.
- docs/wiki/architecture.md says responses pass "byte for byte"; aiter_bytes transparently decompresses gzip (the
  skipped content-encoding keeps it consistent), so the wording overpromises.

If you only do five things: A2 (pool limits vs leases), A3 (validate provider URLs at startup), A5+A6 (Retry-After +
OpenAI-shaped 405/404/500), B2 (health + a few metrics), B4 (make tests runnable without a hand-started Valkey).
