"""Concurrency quotas shared by all gateway replicas, stored in Valkey as expiring leases.

Every running request holds a lease of up to 1 unit of a (virtual key, virtual model) quota.
Leases of one quota live in a sorted set scored by their expiry time. Usage is summed from
the live leases on every acquire, so it never drifts, and leases of a crashed replica
simply expire.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable
from typing import cast
from uuid import uuid4

from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

logger = logging.getLogger(__name__)

MIN_BUDGET = 0.1
"""Smallest capacity a request can run with. A quota with less free capacity is busy."""

# Sweeps expired leases, then takes up to 1 unit of the free capacity.
# KEYS[1]: sorted set of leases, member is "<lease id>|<budget>", score is expiry time.
# ARGV: max_concurrency, min_budget, lease_id, lease_ttl.
_ACQUIRE_SCRIPT = """
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local used = 0
for _, member in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
    used = used + tonumber(string.match(member, '|(.*)$'))
end
local budget = tonumber(ARGV[1]) - used
if budget < tonumber(ARGV[2]) then
    return false
end
local member = ARGV[3] .. '|' .. string.format('%.17g', math.min(1, budget))
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[4]), member)
redis.call('PEXPIRE', KEYS[1], math.ceil((tonumber(ARGV[4]) + 1) * 1000))
return member
"""

# Pushes the expiry of one lease forward. ARGV: member, lease_ttl.
_REFRESH_SCRIPT = """
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], math.ceil((tonumber(ARGV[2]) + 1) * 1000))
"""


class QuotaStore:
    """Hands out quota leases and keeps them alive in background tasks."""

    def __init__(self, valkey: Valkey, lease_ttl: float = 30.0) -> None:
        # `valkey` must be created with `decode_responses=True`
        self._valkey = valkey
        self._lease_ttl = lease_ttl
        self._tasks: set[asyncio.Task[None]] = set()

    async def try_acquire(self, key_name: str, model: str, max_concurrency: float) -> "Lease | None":
        """Take up to 1 unit of free capacity, or return None when less than MIN_BUDGET is free."""
        quota_key = f"llm_gateway:quota:{key_name}:{model}"
        # The tiny tolerance keeps float sums like 0.3 - 0.2 from rejecting a whole MIN_BUDGET
        member = await _eval(
            self._valkey, _ACQUIRE_SCRIPT, quota_key, max_concurrency, MIN_BUDGET - 1e-9, uuid4().hex, self._lease_ttl
        )
        if member is None:
            return None
        lease = Lease(self._valkey, quota_key, member, self._lease_ttl)
        task = asyncio.create_task(lease.keep_alive())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return lease

    async def aclose(self) -> None:
        """Stop keeping leases alive. Leases that were not removed yet expire after their TTL."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Lease:
    """Capacity taken from one quota, held until released and then for the residual time."""

    def __init__(self, valkey: Valkey, quota_key: str, member: str, ttl: float) -> None:
        self.budget = float(member.rpartition("|")[2])
        self._valkey = valkey
        self._quota_key = quota_key
        self._member = member
        self._ttl = ttl
        self._acquired_at = time.monotonic()
        self._released = asyncio.Event()
        self._release_at = 0.0

    def release(self) -> None:
        """Give the capacity back once the residual time has passed. Non-blocking and idempotent."""
        if self._released.is_set():
            return
        # A request running with budget < 1 keeps its capacity longer: twice as long with 0.5
        now = time.monotonic()
        elapsed = now - self._acquired_at
        self._release_at = now + elapsed * (1 - self.budget) / self.budget
        self._released.set()

    async def keep_alive(self) -> None:
        """Refresh the lease until it is released and the residual time has passed, then remove it."""
        interval = self._ttl / 3
        while not await self._wait_released(interval):
            await self._refresh()
        while (remaining := self._release_at - time.monotonic()) > 0:
            await self._refresh()
            await asyncio.sleep(min(remaining, interval))
        try:
            await self._valkey.zrem(self._quota_key, self._member)
        except ValkeyError:
            logger.warning("Failed to remove quota lease %s, it will expire", self._member, exc_info=True)

    async def _wait_released(self, timeout: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._released.wait(), timeout)
        return self._released.is_set()

    async def _refresh(self) -> None:
        try:
            await _eval(self._valkey, _REFRESH_SCRIPT, self._quota_key, self._member, self._ttl)
        except ValkeyError:
            logger.warning("Failed to refresh quota lease %s", self._member, exc_info=True)


async def _eval(valkey: Valkey, script: str, key: str, *args: object) -> str | None:
    """Run a Lua script on one key. Plain EVAL, as a server restart can empty the script cache."""
    # valkey-py annotates commands for its sync and async clients at once
    return await cast(Awaitable[str | None], valkey.eval(script, 1, key, *map(str, args)))
