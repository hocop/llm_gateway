# Architecture

## Modules

| Module | Responsibility |
|---|---|
| `llm_gateway/settings.py` | Runtime settings from environment variables |
| `llm_gateway/config.py` | Loading and cross-checking TOML config; `VirtualKey` access and quota lookups |
| `llm_gateway/quota.py` | Concurrency quotas as leases in Valkey (`QuotaStore`, `Lease`) |
| `llm_gateway/routing.py` | Parsing client requests, fallback between routes under quotas (`ModelRouter`) |
| `llm_gateway/app.py` | FastAPI endpoints, auth, streaming responses back and relabelling them; `create_app_from_env` wires everything |

## API

- `GET /v1/models` lists the virtual models the key may use, OpenAI-style, with extra `description` and
  `quotas` fields. `quotas` holds the key's quotas for the model and for every virtual model it routes to.
- `POST /v1/{path}` forwards a model endpoint to `<provider url>/{path}`. Only the endpoints in
  `_MODEL_ENDPOINTS` are forwarded: `chat/completions`, `completions`, `embeddings`, `responses`, `messages`,
  `rerank`, `score`, `audio/transcriptions`, `audio/translations` and `audio/speech`. Any other path is a 404
  without contacting upstreams, so decoded `..` or `?` can't reach provider admin endpoints. JSON bodies and
  multipart forms are supported; the `model` field is replaced by the real model name and everything else is
  sent unchanged.
- The key is sent as `Authorization: Bearer <secret>`. Gateway errors, unknown paths and methods included, are
  OpenAI-style `{"error": {"message", "type"}}`.

Upstream response bodies, streaming or not, are passed back with their status and headers, so usage and other
extra fields are preserved. Only a compressed body is decompressed, and sent without `Content-Encoding`.

Successful `application/json` and `text/event-stream` bodies are **relabelled**, so clients never see a real
model name: the `model` field becomes the virtual model the client asked for, and the first body carrying one
also gets `last_virtual_model`, the virtual model whose route served the request, and `provider`. Later frames
of a stream get only `model`, keeping them small. Event streams are relabelled line by line, since chunks may
split a frame anywhere. Bodies with no `model` field, audio and other binary bodies, and upstream error
responses are left exactly as they are.

Upstream connections are not pooled with a limit, since quotas already limit concurrency: a request never waits
for a free connection while holding a quota lease. When the client disconnects, Starlette stops the response,
and the upstream connection is closed so that generation stops.

## Routing

`ModelRouter.open` walks the requested virtual model's targets in order, recursing into referenced virtual
models. Before entering a virtual model it takes a lease of the key's quota for it, if the key has one.

A target is skipped when:
- its quota is **busy** (less than `0.1` free), or
- its upstream **failed**: connection error, timeout, or status 404, 408, 429 or 5xx. Other statuses, like
  400, are returned to the client as they are. A failed upstream is not retried within the same request.

The first working upstream wins. If some targets were busy, the request waits for quota (see
[Waiting](#waiting)) and walks again, up to `LLM_GATEWAY_QUOTA_TIMEOUT`, then fails with 429. If every target
failed, the client gets the last upstream error response, or 502 when no upstream responded at all. If Valkey
is unreachable, requests needing a quota fail with 503, while requests without quotas keep working.

Fallback happens only before the response starts. An upstream failing mid-stream aborts the client connection.

## Quotas

Mental model: a key with `max_concurrency = 2.5` for a model has workers with capacities 1, 1 and 0.5. A
worker with capacity `c` that spent `t` seconds on a request idles `t * (1 - c) / c` more seconds before taking
the next one.

Implementation (`quota.py`): every quota is a Valkey sorted set of leases. A member is `<lease id>|<budget>`,
with the budget in integer hundredths, and its score is the lease expiry time (Valkey server time, so replica
clocks don't matter).

1. **Acquire** runs one Lua script, atomic in Valkey: drop expired leases, sum budgets of the rest, set a whole
   unit aside for every request queued earlier (see [Waiting](#waiting)), and, if at least `0.1` is still
   free, add a lease with `budget = min(1, free)`. Usage is recomputed from scratch every time in integers, so
   it is exact.
2. While the request runs, a background task **refreshes** the lease expiry every `ttl / 3` (TTL is 30 s).
3. When the response is fully sent or the client disconnects, the lease is **released**: it is kept, still
   refreshed, for the residual time `elapsed * (1 - budget) / budget`, then removed. The response is not
   delayed by this.
4. If a replica crashes, its leases stop being refreshed and expire within the TTL. On shutdown, pending
   residual times are dropped the same way.

Nested quotas are taken outer to inner, and never held while waiting for another quota, so they can't deadlock.

### Waiting

Requests waiting for the same quota are served in arrival order, and woken up by Valkey pub/sub:

1. A request that finds a quota busy puts a **ticket** in the quota's queue, another sorted set. A member is
   `<arrival time>|<ticket id>`, its score is the ticket expiry time. The ticket is refreshed on every retry.
2. Acquire sets a whole unit aside for every ticket that arrived earlier than the request's own, or for all of
   them if the request has none. Freed capacity goes to the oldest ticket first, and capacity beyond what the
   tickets can take is free for anyone. A ticket leaves a queue when it gets a lease there, and all tickets of
   a request leave once it is routed: served, failed or timed out.
3. A waiting request subscribes to the channels of its queues, named like them. Removing a lease or a ticket
   publishes there, and the waiting requests walk their routes again.
4. Expired leases and tickets send no message, so a waiting request walks again at least every second. A
   ticket expires 3 s after its last retry: its request is gone, or busy trying another route.
5. Leases not used by any response are removed without a message, as requests waiting for nested quotas would
   otherwise wake each other up endlessly.

Pub/sub channels are shared by all databases of a Valkey server, so gateways using different databases of one
server can wake up each other's requests, which only costs them an extra walk.

## Tests

`tests/` runs the app in-process with `httpx.ASGITransport` and mocked providers (`httpx.MockTransport`, see
`tests/helpers.py`). They need a Valkey server at `localhost:6379`, and use its database 15, emptied around every
test:

```sh
uv run pytest
uv run ty check
```
