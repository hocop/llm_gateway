# Agents.md

## Project Overview
An OpenAI-compatible LLM gateway in front of llama.cpp and vLLM servers:
- **Virtual models** rename real models (`provider/model`) and can fall back through a list of targets, including other
  virtual models (`/name`), when a model is unavailable or its quota is busy. Clients only see virtual models.
- **Virtual keys** are configured in TOML, with secrets in `LLM_KEY_<NAME>` environment variables, and are allowed a set
  of virtual models (wildcards supported).
- **Concurrency quotas** per key and virtual model, fractional values included, replace RPM/TPM limits. They are stored
  in Valkey as expiring leases, so all state is shared by replicas.

Streaming, multimodal inputs, embeddings and other model endpoints from an allowlist are passed through unchanged
except for the model name: requests carry the real one to the provider, and successful JSON and event-stream
responses are relabelled back to the virtual model the client asked for, plus `last_virtual_model` and `provider` in `extra_fields`.
Typical load is low (1-20 RPS); robustness and failsafe behavior are the top priority. Design proposals are in
`docs/proposals/`, and the current behavior is described in `docs/wiki/`.

### ⚠ CONSTRAINTS
1. Run commands exactly as prompted here. Do not run `cd <project dir>` before every command. You are in the project root already. Do not add `2>&1`.
2. Never run any `git ...` commands. Never stage (add) or commit your changes. Only human developer is allowed to do that after veryfying all code changes. The only exception is when you are explicitly asked by the user (for example, to fix rebase issues).

---

## Project Structure Guide
```
main.py                   # entry point: runs uvicorn with the app factory
justfile                  # just recipes: check, test, build, up, down
Dockerfile                # gateway image, built with uv; holds no config
docker-compose.yml        # gateway + valkey, for docker compose and podman compose
.dockerignore             # keeps config/, .env, tests and docs out of the image
.env.example              # the LLM_KEY_<NAME> secrets to copy to .env
llm_gateway/
  settings.py             # runtime settings from environment variables
  config.py               # TOML config: providers, virtual models, virtual keys; startup validation
  quota.py                # concurrency quotas as expiring leases in Valkey
  routing.py              # client request parsing, fallback routing under quotas
  app.py                  # FastAPI app: auth, /v1/models, proxy endpoint, streaming and relabelling responses
scripts/
  generate_keys.py        # writes sk-<uuid4> secrets into .env for virtual keys that have none
tests/
  helpers.py              # test config, mocked providers, gateway harness
  conftest.py             # fixtures: test database in local Valkey, in-process gateway
  test_config.py, test_quota.py, test_app.py
config/                   # real config: providers 3090 and stxh, virtual models balanced and smart
examples/config/          # example providers.toml, virtual_models.toml, virtual_keys.toml
docs/proposals/           # design proposals written by the user
docs/wiki/                # up-to-date documentation
docs/change_notes/        # records of big architectural decisions
```

---

## Implementation requirements
1. Python 3.12 with modern typing style.
2. FastAPI as the backend framework.
3. Valkey for all dynamic state, with credentials given in environment variables. Nothing dynamic is kept only in RAM.
4. All usage paths must be covered by unit tests with mocked LLM endpoints. Run them with `just test`.
5. `uv` is the only dependency manager: add dependencies with `uv add` (`uv add --dev` for dev ones). Never run
   `uv pip install` in the venv.
6. `ty` for type checking and `ruff` for linting. Run both with `just check`.

---


## Code quality
1. Preserve good code quality. If you made some changes, but they do not work - revert them.
2. Prioritize readability. Go extra mile to refactor your code to make it readable and conventional.
3. Write short comments on bigger blocks of codes. Do not cover all code in comments, just the essential parts.
4. Prioritize old code over new. Reuse as much as possible. Make sure changes introduced by you are minimal. Make sure your code follows the same conventions and API's as the old code.

## Karpathy Guidelines

Behavioral guidelines to reduce common LLM coding mistakes, derived from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876) on LLM coding pitfalls.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

## Documentation (docs/wiki/)

After every code change, update the relevant file(s) in `docs/wiki/` so they stay in sync with the code (treat this like the `cargo check` step: part of finishing a task, not optional cleanup).

### Docs overview
- `docs/wiki/configuration.md` - running the gateway, environment variables and the TOML config files
- `docs/wiki/architecture.md` - modules, API, routing and fallback rules, how quotas work, tests

Keep this overview list up to date whenever a doc file is added, removed, or renamed.

---

## Change notes (docs/change_notes/)

`docs/change_notes/` records big architectural decisions for future reference. **Do not create a change note automatically.** Only create one when the user explicitly says "note" (or "add a note", etc.) for the change being discussed.

### Format
- File: `docs/change_notes/{i:03n}_{note_name}.md`, indexed from `000` upward (`000_...`, `001_...`, ...). Use the next unused index.
- Sections, in order:
  1. **Why** — the problem/motivation.
  2. **How** — the approach taken.
  3. **What other options were considered** — alternatives and why they were rejected. Do not add that portion if other options weren't explicitly discussed with developer
  4. **What was implemented** — the concrete result (files/systems touched, at a summary level).

### Change notes index
Keep this list up to date every time a note is added, in the format `note_name: one-sentence summary`.

- `000_initial_design.md` - the gateway's initial design: TOML config, fallback routing and concurrency quotas as Valkey leases
- `001_real_valkey_in_tests.md` - tests use database 15 of the local Valkey server instead of fakeredis
- `002_real_providers.md` - real providers 3090 and stxh as virtual models balanced and smart, with quotas tested live
- `003_review_by_qwen_flash.md` - fixes from the qwen review: no connection cap, stricter config validation, OpenAI-style 404/405, more tests
- `004_model_endpoint_allowlist.md` - only allowlisted model endpoints are forwarded, so decoded `..` and `?` in paths can't reach provider admin endpoints
- `005_p001_b1_quota_waiting.md` - requests waiting for quota are served in arrival order through ticket queues and woken up by Valkey pub/sub; budgets are integer hundredths
- `006_p002_compose.md` - deployable with docker/podman compose: an image holding no config, `config/` and `.env` supplied by the host, a Valkey container, and a script generating the missing key secrets

---

## Proposals (docs/proposals/)

`docs/proposals/` holds design proposals the user writes for changes to be implemented later. Proposals are written by the user (not authored by agents), and describe upcoming work before it happens — this is the reverse direction from change notes, which record work after it happens.

- File: `docs/proposals/{i:03n}_{proposal_name}.md`, indexed from `001` upward, matching the format of the change notes index.
- A change note that implements a proposal must refer to it via its index. E.g. `docs/proposals/001_foo.md` is implemented and documented in `docs/change_notes/001_p001_foo.md`. Note and proposal indexes are not always the same.
- When asked to implement a proposal, read the corresponding file in `docs/proposals/` fully first, and treat it as the source of truth for the design — ask the user if anything in it is ambiguous or looks outdated relative to the current code.
- Do not edit files in `docs/proposals/` unless the user explicitly asks; they are the user's own notes.

---

# How to keep AGENTS.md up to date
1. When files change, update the project tree description
2. Update the docs overview and change notes index above whenever `docs/wiki/` or `docs/change_notes/` change (see those sections)
3. Leave everything else the same
4. If you see anything out-of-date except the file structure, docs overview, or change notes index, ask user for permission to change it
