# Configuration

## Running

The main path is compose, with `docker compose` and `podman compose` alike:

```sh
podman compose up -d
```

`docker-compose.yml` runs the gateway on port 4000 and a Valkey container next to it (`VALKEY_URL` points the
gateway at it). Valkey saves nothing to disk: quota leases are short-lived, and losing them only frees quota.

Two things are mounted or read from the host, and the image holds neither:

- `./config` is mounted read-only at `/app/config`. Without it the gateway finds no config files and exits.
- `.env` next to `docker-compose.yml` holds the `LLM_KEY_<NAME>` secrets. Compose fails to start if it is missing.

Without compose:

```sh
uv run main.py --host 0.0.0.0 --port 4000 --workers 2
```

`main.py` starts uvicorn with the `llm_gateway.app:create_app_from_env` factory. Replicas and workers share
all quota state through Valkey, so any number of them can run at once.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LLM_GATEWAY_CONFIG_DIR` | `config` | Directory with the three TOML files below |
| `VALKEY_URL` | `valkey://localhost:6379/0` | Valkey connection, credentials included: `valkey://:password@host:6379/0` |
| `LLM_GATEWAY_QUOTA_TIMEOUT` | `300` | Seconds a request waits for free quota before getting 429 |
| `LLM_GATEWAY_UPSTREAM_TIMEOUT` | `600` | Seconds of upstream silence (waiting for headers or the next chunk) before the upstream counts as failed |
| `LLM_KEY_<NAME>` | required | Secret of the virtual key `<name>`, e.g. `LLM_KEY_MY_SERVICE_1` for `my_service_1` |

`uv run scripts/generate_keys.py` writes a secret into `.env` for every virtual key in the config that has none,
as `sk-<uuid4>`. Secrets already in the file are never changed, so it is safe to rerun after adding a key. Any
non-empty secret works; the gateway only requires that two keys do not share one.

## Config files

Everything is validated at startup and the gateway refuses to start on any error: unknown fields, duplicate
names, provider URLs without `http://` or `https://`, unknown providers or virtual models, key model patterns
matching no virtual model, reference cycles, quotas below `0.1` or with more than two decimals, missing or shared key secrets. See `examples/config/` for a complete example.

Python's `tomllib` reads TOML 1.0, where an inline table `{ ... }` must fit on one line.

### `providers.toml`

```toml
[[providers]]
name = "vllm"
url = "http://localhost:8000/v1"  # the path of an allowed model endpoint is appended, e.g. /chat/completions
key = "my-secret-key"             # optional, sent as "Authorization: Bearer ..."; empty means none
```

### `virtual_models.toml`

```toml
[[virtual_models]]
name = "fast_model"
model = "vllm/qwen3.8_27B-fp8"  # provider/real_model; only the first slash separates them
description = "Fast model for everyday use"

[[virtual_models]]
name = "first_available"
model = ["/fast_model", "llama_cpp/qwen3.8_flash_next-GGUF"]  # tried in order
description = "Fast model, or smart model when fast is unavailable"
```

`model` is one target or a list of targets tried in order. A target with an empty provider, like
`/fast_model`, refers to another virtual model of this gateway. Only virtual models are visible to clients.

### `virtual_keys.toml`

```toml
[[virtual_keys]]
name = "my_service_1"  # lowercase letters, digits and underscores
models = ["first_available"]  # names or wildcard patterns, e.g. "*" or ["fast_*"]
quotas = [
    { model = "fast_model", max_concurrency = 1.5 },
    { model = "smart_model", max_concurrency = 0.5 },
]
```

- `models` lists the virtual models the key may request directly. Access to `first_available` also allows using the
  models it routes to *through* it.
- `quotas` limit concurrency per virtual model, including models reached through references: above,
  `my_service_1` requesting `first_available` is limited by the `fast_model` quota while routed to `fast_model`.
  A model without a quota is unlimited for the key. How `max_concurrency` works is described in
  [architecture.md](architecture.md#quotas).
