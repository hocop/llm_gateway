# 001 Real Valkey in tests

## Why

Tests ran quotas on fakeredis with Lua support, because no Valkey server was available locally when the gateway was
first built (see `000_initial_design.md`). fakeredis emulates Valkey, so the Lua scripts, `TIME`, sorted set expiry
and the valkey-py client were never tested against the server the gateway runs on. A Valkey server is now
installed on the development machine, so tests can use it.

## How

- The `valkey` fixture in `tests/conftest.py` connects to `valkey://localhost:6379/15`, with `decode_responses=True`
  like the app. It uses the same `valkey.asyncio.Valkey` client as `llm_gateway/app.py`.
- Database 15 belongs to tests. The fixture runs `FLUSHDB` before and after every test, so each test starts empty
  and assertions like "no keys are left" still hold.
- fakeredis is removed. Nothing else in the tests changed: they already used only the client API, and
  `monkeypatch` on `eval` works on a real client too.

## What other options were considered

- **Keeping fakeredis**: rejected. It is an emulation, and a real server was now available.
- **fakeredis by default, real Valkey on request**: rejected. Two ways to run the same tests, where the default one
  still doesn't test the real server.
- **Isolating tests by key prefix instead of flushing a database**: rejected. `QuotaStore` has a fixed
  `llm_gateway:quota:` prefix, and a test checks that no keys exist at all. A separate database needs no code change.
- **Using database 0**: rejected. It is the gateway's default `VALKEY_URL`, and flushing it would wipe the leases of
  a gateway running locally.
- **An environment variable for the test Valkey URL**: not added, as no other setup needs it yet. The URL is a
  constant in `tests/conftest.py`.
- **Switching quota scripts to `EVALSHA`**: not revisited. The initial design rejected it partly because fakeredis
  didn't support valkey-py's `NoScriptError` fallback. The other reason still holds: a Valkey restart flushes the
  script cache.

The cost of this change: tests need a Valkey server at `localhost:6379`, its database 15 is emptied on every run,
and two test runs at the same time would share that database.

## What was implemented

- `tests/conftest.py`: the `valkey` fixture on the local Valkey database 15, flushed around every test.
- `tests/helpers.py`, `tests/test_quota.py`: `FakeAsyncValkey` type hints replaced by `valkey.asyncio.Valkey`.
- `pyproject.toml`, `uv.lock`: `fakeredis` dev dependency removed, together with `lupa`, `redis` and
  `sortedcontainers`.
- `docs/wiki/architecture.md` (Tests section) and `AGENTS.md` (project structure) updated.
- All 55 tests pass against the real server, in 5 runs in a row, and `uv run ty check` passes.
