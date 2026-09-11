# Architecture

## Modules

| Module | Responsibility |
|---|---|
| `llm_gateway/settings.py` | Runtime settings from environment variables |
| `llm_gateway/config.py` | Loading and cross-checking TOML config; `VirtualKey` access and quota lookups |
| `llm_gateway/quota.py` | Concurrency quotas as leases in Valkey (`QuotaStore`, `Lease`) |
| `llm_gateway/routing.py` | Parsing client requests, fallback between routes under quotas (`ModelRouter`) |
| `llm_gateway/app.py` | FastAPI endpoints, auth, streaming responses back; `create_app_from_env` wires everything |

## API

- `GET /v1/models` lists the virtual models the key may use, OpenAI-style, with extra `description` and
  `quotas` fields. `quotas` holds the key's quotas for the model and for every virtual model it routes to.
- `POST /v1/{path}` forwards any model endpoint (`chat/completions`, `completions`, `embeddings`,
  `audio/transcriptions`, `rerank`, ...) to `<provider url>/{path}`. JSON bodies and multipart forms are
  supported; the `model` field is replaced by the real model name and everything else is sent unchanged.
- The key is sent as `Authorization: Bearer <secret>`. Gateway errors are OpenAI-style
  `{"error": {"message", "type"}}`.

Upstream responses, streaming or not, are passed back byte for byte with their status and headers, so usage
and other extra fields are preserved. The `model` field in responses keeps the real model name.

## Routing

`ModelRouter.open` walks the requested virtual model's targets in order, recursing into referenced virtual
models. Before entering a virtual model it takes a lease of the key's quota for it, if the key has one.

A target is skipped when:
- its quota is **busy** (less than `0.1` free), or
- its upstream **failed**: connection error, timeout, or status 404, 408, 429 or 5xx. Other statuses, like
  400, are returned to the client as they are. A failed upstream is not retried within the same request.

The first working upstream wins. If some targets were busy, the whole walk repeats every 0.2 s until
`LLM_GATEWAY_QUOTA_TIMEOUT`, then fails with 429. If every target failed, the client gets the last upstream
error response, or 502 when no upstream responded at all. If Valkey is unreachable, requests needing a quota
fail with 503, while requests without quotas keep working.

Fallback happens only before the response starts. An upstream failing mid-stream aborts the client connection.

## Quotas

Mental model: a key with `max_concurrency = 2.5` for a model has workers with capacities 1, 1 and 0.5. A
worker with capacity `c` that spent `t` seconds on a request idles `t * (1 - c) / c` more seconds before taking
the next one.

Implementation (`quota.py`): every quota is a Valkey sorted set of leases. A member is `<lease id>|<budget>`,
its score is the lease expiry time (Valkey server time, so replica clocks don't matter).

1. **Acquire** runs one Lua script, atomic in Valkey: drop expired leases, sum budgets of the rest, and, if at
   least `0.1` is free, add a lease with `budget = min(1, free)`. Usage is recomputed from scratch every time,
   so floating point errors never accumulate.
2. While the request runs, a background task **refreshes** the lease expiry every `ttl / 3` (TTL is 30 s).
3. When the response is fully sent or the client disconnects, the lease is **released**: it is kept, still
   refreshed, for the residual time `elapsed * (1 - budget) / budget`, then removed. The response is not
   delayed by this.
4. If a replica crashes, its leases stop being refreshed and expire within the TTL. On shutdown, pending
   residual times are dropped the same way.

Nested quotas are taken outer to inner, and never held while waiting for another quota, so they can't deadlock.
Waiting is polling: requests waiting for the same quota are served in no particular order.

## Tests

`tests/` runs the app in-process with `httpx.ASGITransport`, mocked providers (`httpx.MockTransport`, see
`tests/helpers.py`) and `fakeredis` with Lua support in place of Valkey:

```sh
uv run pytest
uv run ty check
```
