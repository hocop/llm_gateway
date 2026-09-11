import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from valkey.asyncio import Valkey
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from llm_gateway.quota import Lease, QuotaStore
from tests.helpers import eventually

LEASE_TTL = 0.3


@pytest.fixture
async def store(valkey: Valkey) -> AsyncIterator[QuotaStore]:
    quotas = QuotaStore(valkey, lease_ttl=LEASE_TTL)
    yield quotas
    await quotas.aclose()


async def acquire(store: QuotaStore, max_concurrency: float, key: str = "key", model: str = "model") -> Lease:
    lease = await store.try_acquire(key, model, max_concurrency)
    assert lease is not None
    return lease


async def test_capacity_is_split_into_leases_of_at_most_one(store: QuotaStore) -> None:
    budgets = [(await acquire(store, 2.5)).budget for _ in range(3)]

    assert budgets == [1, 1, 0.5]
    assert await store.try_acquire("key", "model", 2.5) is None


async def test_capacity_below_min_budget_is_busy(store: QuotaStore) -> None:
    assert (await acquire(store, 1.05)).budget == 1
    assert await store.try_acquire("key", "model", 1.05) is None


async def test_quotas_are_separate_per_key_and_model(store: QuotaStore) -> None:
    await acquire(store, 1, key="a", model="m")
    await acquire(store, 1, key="b", model="m")
    await acquire(store, 1, key="a", model="n")

    assert await store.try_acquire("a", "m", 1) is None


async def test_released_lease_frees_capacity(store: QuotaStore) -> None:
    lease = await acquire(store, 1)
    lease.release()
    lease.release()  # releasing twice is harmless

    async def is_free() -> bool:
        return await store.try_acquire("key", "model", 1) is not None

    await eventually(is_free)


async def test_fractional_lease_is_held_for_residual_time(store: QuotaStore) -> None:
    lease = await acquire(store, 0.5)
    await asyncio.sleep(0.2)  # the request runs, and the residual time is as long
    lease.release()
    released_at = time.monotonic()

    async def is_free() -> bool:
        return await store.try_acquire("key", "model", 0.5) is not None

    await eventually(is_free)
    assert time.monotonic() - released_at >= 0.19


async def test_heartbeat_keeps_running_request_leased(store: QuotaStore) -> None:
    lease = await acquire(store, 1)
    await asyncio.sleep(LEASE_TTL * 2)

    assert await store.try_acquire("key", "model", 1) is None
    lease.release()


async def test_lease_outlives_quota_store_outage(
    store: QuotaStore, valkey: Valkey, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(*args: object) -> None:
        raise ValkeyConnectionError("Connection refused")

    lease = await acquire(store, 1)
    monkeypatch.setattr(valkey, "eval", fail)
    await asyncio.sleep(LEASE_TTL / 2)  # a refresh fails
    monkeypatch.undo()
    await asyncio.sleep(LEASE_TTL)  # refreshes resumed, so the lease didn't expire

    assert await store.try_acquire("key", "model", 1) is None
    monkeypatch.setattr(valkey, "zrem", fail)
    lease.release()

    async def is_free() -> bool:
        return await store.try_acquire("key", "model", 1) is not None

    await eventually(is_free)  # the lease that failed to be removed expires
    assert "Failed to refresh quota lease" in caplog.text
    assert "Failed to remove quota lease" in caplog.text


async def test_leases_of_crashed_replica_expire(valkey: Valkey) -> None:
    crashed = QuotaStore(valkey, lease_ttl=LEASE_TTL)
    await acquire(crashed, 1)
    await crashed.aclose()  # heartbeats stop without releasing the lease

    healthy = QuotaStore(valkey, lease_ttl=LEASE_TTL)
    assert await healthy.try_acquire("key", "model", 1) is None
    await asyncio.sleep(LEASE_TTL + 0.05)
    assert await healthy.try_acquire("key", "model", 1) is not None
    await healthy.aclose()
