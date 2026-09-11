# 002 Real providers

## Why

The gateway had only example config and unit tests with mocked upstreams (see `000_initial_design.md`), so it had
never run against the real LLM servers on the LAN. Quotas were verified only against mocked providers, with requests
lasting milliseconds, never with real generation times of seconds to minutes.

## How

- Two real providers: `3090` at `http://10.7.0.3:11434/v1` (vLLM, Qwen3.8-27B-FP8) and `stxh` at
  `http://10.7.0.4:8080/v1` (llama.cpp, a ~177B GGUF). Both servers name their model `llm_at_home` and need no key.
- Virtual models `balanced` → `3090/llm_at_home` and `smart` → `stxh/llm_at_home`.
- One virtual key `me` with access to all models and quotas `balanced = 1.5` (whole and fractional capacity) and
  `smart = 0.5` (fractional only). The values were chosen for the test; no limits were requested.
- The config lives in `config/`, the default `LLM_GATEWAY_CONFIG_DIR`. It holds no secrets: `LLM_KEY_ME` comes from
  the environment.
- Quotas were tested live: the gateway ran on `localhost:4000` with Valkey database 0, and a script sent staggered
  streaming requests while polling the quota sorted sets in Valkey every 0.05 s. It checked that the sum of lease
  budgets never exceeds `max_concurrency`, and that a lease with budget `b` is held for `run time / b`.

## What other options were considered

- **Putting the real config in `examples/config/`**: rejected. The examples document the format with generic names,
  and `config/` is what the gateway reads without extra settings.
- **Adding the live test to `tests/`**: rejected. Unit tests must use mocked endpoints, and this one depends on
  servers on the LAN and takes about two minutes. The script was kept out of the repo.
- **Judging admission by response headers only**: rejected. Headers arrive after upstream latency is added, so
  lease budgets and hold times were read from Valkey directly, with request timings only as a cross-check.
- **Running the live gateway on database 15**: rejected. It belongs to unit tests and is flushed on every run.

## What was implemented

- `config/providers.toml`, `config/virtual_models.toml`, `config/virtual_keys.toml`.
- `AGENTS.md` (project structure) updated.
- Live results, all requests 200, no warnings in the gateway log:
  - `balanced` (1.5), 4 requests of 300 tokens: the first two got budgets 1.0 and 0.5 at once; the third waited
    46.7 s for the 1.0 lease to end and got 1.0; the fourth got 1.0 when the third finished. Usage peaked at 1.50,
    and the 0.5 lease was held 97.8 s for a 48.9 s request.
  - `smart` (0.5), 2 requests of 40 tokens: the first ran 6.4 s and held its lease 13.8 s; the second started only
    after that.
  - Every lease's hold time matched `run time / budget` within 0.05 s.
- Not tested live: fallback between models (none is configured) and the 429 after `LLM_GATEWAY_QUOTA_TIMEOUT`.
