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
    lease = await store.try_acquire(key, model, max_concurrency, store.ticket())
    assert lease is not None
    return lease


async def test_capacity_is_split_into_leases_of_at_most_one(store: QuotaStore) -> None:
    budgets = [(await acquire(store, 2.5)).budget for _ in range(3)]

    assert budgets == [1, 1, 0.5]
    assert await store.try_acquire("key", "model", 2.5, store.ticket()) is None


async def test_capacity_is_counted_exactly(store: QuotaStore) -> None:
    budgets = [(await acquire(store, 4.1)).budget for _ in range(5)]

    assert budgets == [1, 1, 1, 1, 0.1]  # while 4.1 - 4 < 0.1 in floating point


async def test_capacity_below_min_budget_is_busy(store: QuotaStore) -> None:
    assert (await acquire(store, 1.05)).budget == 1
    assert await store.try_acquire("key", "model", 1.05, store.ticket()) is None


async def test_quotas_are_separate_per_key_and_model(store: QuotaStore) -> None:
    await acquire(store, 1, key="a", model="m")
    await acquire(store, 1, key="b", model="m")
    await acquire(store, 1, key="a", model="n")

    assert await store.try_acquire("a", "m", 1, store.ticket()) is None


async def test_released_lease_frees_capacity(store: QuotaStore) -> None:
    lease = await acquire(store, 1)
    lease.release()
    lease.release()  # releasing twice is harmless
    ticket = store.ticket()

    async def is_free() -> bool:
        return await store.try_acquire("key", "model", 1, ticket) is not None

    await eventually(is_free)


async def test_fractional_lease_is_held_for_residual_time(store: QuotaStore) -> None:
    lease = await acquire(store, 0.5)
    await asyncio.sleep(0.2)  # the request runs, and the residual time is as long
    lease.release()
    released_at = time.monotonic()
    ticket = store.ticket()

    async def is_free() -> bool:
        return await store.try_acquire("key", "model", 0.5, ticket) is not None

    await eventually(is_free)
    assert time.monotonic() - released_at >= 0.19


async def test_heartbeat_keeps_running_request_leased(store: QuotaStore) -> None:
    lease = await acquire(store, 1)
    await asyncio.sleep(LEASE_TTL * 2)

    assert await store.try_acquire("key", "model", 1, store.ticket()) is None
    lease.release()


async def test_lease_outlives_quota_store_outage(
    store: QuotaStore, valkey: Valkey, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(*args: object) -> None:
        raise ValkeyConnectionError("Connection refused")

    lease = await acquire(store, 1)
    ticket = store.ticket()
    monkeypatch.setattr(valkey, "eval", fail)
    await asyncio.sleep(LEASE_TTL / 2)  # a refresh fails
    monkeypatch.undo()
    await asyncio.sleep(LEASE_TTL)  # refreshes resumed, so the lease didn't expire

    assert await store.try_acquire("key", "model", 1, ticket) is None
    monkeypatch.setattr(valkey, "zrem", fail)
    lease.release()

    async def is_free() -> bool:
        return await store.try_acquire("key", "model", 1, ticket) is not None

    await eventually(is_free)  # the lease that failed to be removed expires
    assert "Failed to refresh quota lease" in caplog.text
    assert "Failed to remove quota lease" in caplog.text


async def test_leases_of_crashed_replica_expire(valkey: Valkey) -> None:
    crashed = QuotaStore(valkey, lease_ttl=LEASE_TTL)
    await acquire(crashed, 1)
    await crashed.aclose()  # heartbeats stop without releasing the lease

    healthy = QuotaStore(valkey, lease_ttl=LEASE_TTL)
    ticket = healthy.ticket()
    assert await healthy.try_acquire("key", "model", 1, ticket) is None
    await asyncio.sleep(LEASE_TTL + 0.05)
    assert await healthy.try_acquire("key", "model", 1, ticket) is not None
    await healthy.aclose()


async def test_waiting_tickets_get_capacity_in_arrival_order(store: QuotaStore, valkey: Valkey) -> None:
    lease = await acquire(store, 1)
    first, second = store.ticket(), store.ticket()
    assert await store.try_acquire("key", "model", 1, first) is None
    assert await store.try_acquire("key", "model", 1, second) is None
    lease.release()
    await eventually(lambda: _lease_count_is(valkey, 0))

    assert await store.try_acquire("key", "model", 1, second) is None
    assert await store.try_acquire("key", "model", 1, store.ticket()) is None  # new requests queue up too
    assert await store.try_acquire("key", "model", 1, first) is not None


async def test_ticket_leaving_queue_lets_next_one_in(store: QuotaStore, valkey: Valkey) -> None:
    lease = await acquire(store, 1)
    first, second = store.ticket(), store.ticket()
    assert await store.try_acquire("key", "model", 1, first) is None
    assert await store.try_acquire("key", "model", 1, second) is None
    lease.release()
    await eventually(lambda: _lease_count_is(valkey, 0))

    await first.aclose()  # e.g. its request was served by another model
    assert await store.try_acquire("key", "model", 1, second) is not None


async def test_capacity_beyond_waiting_tickets_is_free_for_others(store: QuotaStore, valkey: Valkey) -> None:
    leases = [await acquire(store, 2.5) for _ in range(3)]
    waiting = store.ticket()
    assert await store.try_acquire("key", "model", 2.5, waiting) is None
    leases[0].release()
    leases[2].release()
    await eventually(lambda: _lease_count_is(valkey, 1))

    # 1.5 is free, and a whole unit of it is set aside for the waiting ticket
    assert (await acquire(store, 2.5)).budget == 0.5
    lease = await store.try_acquire("key", "model", 2.5, waiting)
    assert lease is not None and lease.budget == 1


async def test_ticket_of_gone_request_expires(valkey: Valkey) -> None:
    store = QuotaStore(valkey, lease_ttl=LEASE_TTL, poll_interval=0.05)
    lease = await acquire(store, 1)
    gone, waiting = store.ticket(), store.ticket()
    assert await store.try_acquire("key", "model", 1, gone) is None
    queued_at = time.monotonic()
    assert await store.try_acquire("key", "model", 1, waiting) is None
    lease.release()  # the first request never retries, nor leaves the queue

    async def is_free() -> bool:
        return await store.try_acquire("key", "model", 1, waiting) is not None

    await eventually(is_free)
    assert time.monotonic() - queued_at >= 0.14  # tickets expire after 3 poll intervals
    await store.aclose()


async def test_waiting_ticket_wakes_up_when_lease_is_removed(valkey: Valkey) -> None:
    store = QuotaStore(valkey, lease_ttl=LEASE_TTL, poll_interval=10)
    unused, used = await acquire(store, 2), await acquire(store, 2)
    ticket = store.ticket()
    assert await store.try_acquire("key", "model", 2, ticket) is None
    async with asyncio.timeout(1):
        await ticket.wait(10)  # subscribes and returns at once, for the caller to retry

    unused.release(wake_waiters=False)
    started = time.monotonic()
    await ticket.wait(0.3)
    assert time.monotonic() - started >= 0.29

    used.release()
    async with asyncio.timeout(1):
        await ticket.wait(10)
    await ticket.aclose()
    await store.aclose()


async def _lease_count_is(valkey: Valkey, count: int) -> bool:
    return await valkey.zcard("llm_gateway:quota:key:model") == count
