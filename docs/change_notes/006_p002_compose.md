# 006 Compose deployment

Implements `docs/proposals/002_compose.md`.

## Why

The gateway could only be started by hand with `uv run main.py`, next to a Valkey server the operator had to run
themselves. The proposal asks for an image and a compose file, so that deploying is one command, and for a README
written for someone who has never seen the repo: the project is being open sourced.

## How

- `Dockerfile` on `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`. Dependencies are installed from `uv.lock` with
  `uv sync --no-dev --frozen` before the source is copied, so editing code does not reinstall them. The project has
  no `[build-system]`, so `uv sync` installs dependencies only and `main.py` runs from `/app`.
- `docker-compose.yml` runs the gateway and a Valkey container. Valkey saves nothing to disk (`--save ""`,
  `--appendonly no`): leases are short-lived, and losing them only frees quota.
- Both `docker compose` and `podman compose` read it: image names are fully qualified, since podman has no default
  registry; there is no obsolete `version:` key; and `depends_on` is the plain list form rather than a healthcheck
  condition, which podman-compose supports unevenly. The gateway tolerates Valkey being slow to start anyway.
- The image holds no config. `./config` is mounted read-only at `/app/config`, and `.env` supplies the
  `LLM_KEY_<NAME>` secrets. Without the mount the gateway exits at startup; without `.env` compose refuses to start.
- `config/` is the operator's own config and stays gitignored, so a fresh clone starts from `examples/config/` and
  `.env.example` instead.
- `scripts/generate_keys.py` writes a `sk-<uuid4>` secret into `.env` for every virtual key that has none, and never
  changes the ones already there, so it can be rerun after adding a key. The gateway validates no secret format; it
  only requires a non-empty secret that no other key uses. The script is standalone stdlib code that imports nothing
  from `llm_gateway`, so `uv run scripts/generate_keys.py` works with no package on the path; a config directory
  elsewhere is given with `--config-dir`.

## What other options were considered

- **Resolving dependencies at build time** (`uv sync` with no lockfile, leaving `uv.lock` gitignored): rejected by
  the user. It builds from any clone, but two builds weeks apart can pick different versions. `uv.lock` was
  un-ignored instead, and the image is built `--frozen`.
- **Testing with docker rather than podman**: impossible, docker is not installed. Podman and podman-compose were
  installed for the test, as the proposal asks.
- **Reusing `KEY_ENV_PREFIX` and `Settings` from the package in the key script**: rejected by the user. The project
  has no `[build-system]` and so is not installed in the venv, and a directly run script puts `scripts/` on
  `sys.path` instead of the project root, so importing `llm_gateway` left the script runnable only as
  `uv run python -m scripts.generate_keys`. Standalone, it restates `LLM_KEY_` in one line and simply runs.

## What was implemented

- New: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `scripts/generate_keys.py`.
- `README.md` rewritten: what the service is, config copied from `examples/config/`, compose as the main launch
  path with the warning that config must be mounted, and links to the wiki for the details.
- `docs/wiki/configuration.md`: compose is now the documented way to run, with the plain `uv run` path kept below.
- `.gitignore`: `uv.lock` is tracked now; `config/` and `.env` stay ignored.
- `pyproject.toml`: `[tool.ruff] line-length = 120`. Ruff on its default 88 reported only import blocks that are
  already sorted but longer than 88, and would have rewrapped four of them against the style of the whole codebase.
- `tests/test_app.py`: two nested `async with` combined into one (ruff SIM117).
- `scripts/generate_keys.py` checked by hand against a partial `.env` (existing secrets and unrelated variables kept,
  a commented-out key treated as unset), a file that does not exist yet, a rerun (no-op) and the real `.env` (left
  byte for byte identical). It has no unit tests: it touches no gateway code and is not on any request path.
- Verified with podman 6.0.0 and podman-compose 1.6.0: the image builds, both containers start, `/app/config` holds
  the mounted files, `/v1/models` lists the real virtual models with their quotas, an unknown key gets 401, and a
  chat completion reached the vLLM provider and came back through the gateway. `docker compose` is untested, as
  docker is not installed on this machine.
