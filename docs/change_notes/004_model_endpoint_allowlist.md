# 004 Model endpoint allowlist

## Why

`POST /v1/{path:path}` forwarded any path, appended as is to the provider URL: `f"{provider.url}/{path}"`. The
path is not safe to append. uvicorn decodes `%2F`, `%2E` and `%3F` before routing, Starlette's `path` converter
accepts `..`, and httpx removes dot segments when it parses the built URL. Checked against Starlette 1.6, uvicorn
0.52 and httpx 0.28 with a one-off script, a real uvicorn gateway and an upstream echoing what it received:

| Client sends | Upstream receives |
|---|---|
| `/v1/../admin` | `/admin` |
| `/v1/%2E%2E/%2E%2E/metrics` | `/metrics` |
| `/v1/..%2F..%2Fsleep` | `/sleep` |
| `/v1/chat/completions%3Fsecret=1` | `/v1/chat/completions?secret=1` |

The upstream host can't change, and a caller needs a valid virtual key allowed the requested model. But such a
key could POST to any path of the providers, with the provider key attached, through the gateway's network
access: vLLM's `/sleep`, `/reset_prefix_cache`, `/start_profile`, llama.cpp's `/slots/{id}?action=...`,
`/lora-adapters`. Even without traversal, non-model endpoints under `/v1`, like vLLM's `/v1/load_lora_adapter`,
were forwarded.

## How

`proxy` checks the path against `_MODEL_ENDPOINTS`, a fixed set of exact paths, before reading the body. Any other
path is a 404 `not_found_error`, and no upstream is contacted. Authentication still comes first.

The set holds the model endpoints llama.cpp and vLLM serve under `/v1`, all taking a `model` field:
`chat/completions`, `completions`, `embeddings`, `responses`, `messages`, `rerank`, `score`,
`audio/transcriptions`, `audio/translations`, `audio/speech`. A new endpoint is supported by adding it there.

## What was implemented

- `llm_gateway/app.py`: `_MODEL_ENDPOINTS`, checked first in `proxy`.
- `tests/test_app.py`: `test_endpoint_outside_allowlist_is_not_forwarded` with encoded traversal, an encoded `?`
  and `load_lora_adapter`. All 4 cases reached the mocked upstream with 200 before the fix. 71 tests pass;
  `uv run ty check` passes.
- `docs/wiki/architecture.md`, `docs/wiki/configuration.md`, `AGENTS.md` updated.
