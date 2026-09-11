"""Concurrency quotas shared by all gateway replicas, stored in Valkey as expiring leases.

Every running request holds a lease of up to 1 unit of a (virtual key, virtual model) quota.
Leases of one quota live in a sorted set scored by their expiry time. Usage is summed from
the live leases on every acquire, so it never drifts, and leases of a crashed replica
simply expire.

Requests waiting for a busy quota hold tickets in its queue, another sorted set, and get
capacity in arrival order. Removing a lease publishes a message that wakes them up.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Sequence
from typing import Any, cast
from uuid import uuid4

from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

logger = logging.getLogger(__name__)

MIN_BUDGET = 0.1
"""Smallest capacity a request can run with. A quota with less free capacity is busy."""

# Sweeps expired leases and tickets. Then takes up to 1 unit of the capacity left by tickets queued earlier,
# or else queues the ticket. Capacity is counted in integer hundredths, so sums are exact.
# KEYS[1]: sorted set of leases, member is "<lease id>|<budget>", score is expiry time.
# KEYS[2]: sorted set of tickets, member is "<arrival time in microseconds>|<ticket id>", score is expiry time.
# ARGV: max_concurrency, min_budget, lease_id, lease_ttl, ticket member or "", ticket_id, ticket_ttl.
# Returns {"lease", lease member} or {"queued", ticket member}.
_ACQUIRE_SCRIPT = """
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local free = tonumber(ARGV[1])
for _, member in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
    free = free - tonumber(string.match(member, '|(.*)$'))
end
-- A new or expired ticket arrives now. Every ticket that arrived earlier takes a whole unit first
local ticket = ARGV[5]
if not redis.call('ZSCORE', KEYS[2], ticket) then
    ticket = time[1] .. string.format('%06d', time[2]) .. '|' .. ARGV[6]
end
local arrival = tonumber(string.match(ticket, '^(%d+)'))
for _, member in ipairs(redis.call('ZRANGE', KEYS[2], 0, -1)) do
    if tonumber(string.match(member, '^(%d+)')) < arrival then
        free = free - 100
    end
end
if free < tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[2], now + tonumber(ARGV[7]), ticket)
    redis.call('PEXPIRE', KEYS[2], math.ceil((tonumber(ARGV[7]) + 1) * 1000))
    return {'queued', ticket}
end
redis.call('ZREM', KEYS[2], ticket)
local member = ARGV[3] .. '|' .. math.min(100, free)
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[4]), member)
redis.call('PEXPIRE', KEYS[1], math.ceil((tonumber(ARGV[4]) + 1) * 1000))
return {'lease', member}
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

    def __init__(self, valkey: Valkey, lease_ttl: float = 30.0, poll_interval: float = 1.0) -> None:
        # `valkey` must be created with `decode_responses=True`
        self._valkey = valkey
        self._lease_ttl = lease_ttl
        self._poll_interval = poll_interval
        # Waiting requests retry at least every poll interval. A ticket not retried for longer belongs to
        # a request that is gone, or busy trying another route
        self._ticket_ttl = 3 * poll_interval
        self._tasks: set[asyncio.Task[None]] = set()

    def ticket(self) -> "Ticket":
        """A new ticket for one request to wait for quotas with. Close it once the request is routed."""
        return Ticket(self._valkey, self._poll_interval)

    async def try_acquire(self, key_name: str, model: str, max_concurrency: float, ticket: "Ticket") -> "Lease | None":
        """Take up to 1 unit of free capacity, or queue the ticket and return None when less than MIN_BUDGET is free.

        Tickets queued earlier take a whole unit each first.
        """
        quota_key = f"llm_gateway:quota:{key_name}:{model}"
        queue_key = f"llm_gateway:queue:{key_name}:{model}"
        kind, member = await _eval(
            self._valkey,
            _ACQUIRE_SCRIPT,
            [quota_key, queue_key],
            _hundredths(max_concurrency),
            _hundredths(MIN_BUDGET),
            uuid4().hex,
            self._lease_ttl,
            ticket._members.get(queue_key, ""),
            ticket.id,
            self._ticket_ttl,
        )
        if kind == "queued":
            ticket._members[queue_key] = member
            return None
        lease = Lease(self._valkey, quota_key, queue_key, member, self._lease_ttl)
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

    def __init__(self, valkey: Valkey, quota_key: str, queue_key: str, member: str, ttl: float) -> None:
        self.budget = int(member.rpartition("|")[2]) / 100
        self._valkey = valkey
        self._quota_key = quota_key
        self._queue_key = queue_key
        self._member = member
        self._ttl = ttl
        self._acquired_at = time.monotonic()
        self._released = asyncio.Event()
        self._release_at = 0.0
        self._wake_waiters = True

    def release(self, wake_waiters: bool = True) -> None:
        """Give the capacity back once the residual time has passed. Non-blocking and idempotent.

        Requests waiting for the quota are woken up then, unless `wake_waiters` is false.
        """
        if self._released.is_set():
            return
        # A request running with budget < 1 keeps its capacity longer: twice as long with 0.5
        now = time.monotonic()
        elapsed = now - self._acquired_at
        self._release_at = now + elapsed * (1 - self.budget) / self.budget
        self._wake_waiters = wake_waiters
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
            if await self._valkey.zrem(self._quota_key, self._member) and self._wake_waiters:
                await self._valkey.publish(self._queue_key, "")
        except ValkeyError:
            logger.warning("Failed to remove quota lease %s, it will expire", self._member, exc_info=True)

    async def _wait_released(self, timeout: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._released.wait(), timeout)
        return self._released.is_set()

    async def _refresh(self) -> None:
        try:
            await _eval(self._valkey, _REFRESH_SCRIPT, [self._quota_key], self._member, self._ttl)
        except ValkeyError:
            logger.warning("Failed to refresh quota lease %s", self._member, exc_info=True)


class Ticket:
    """A request's places in the queues of busy quotas, held while it waits for any of them."""

    def __init__(self, valkey: Valkey, poll_interval: float) -> None:
        self.id = uuid4().hex
        self._valkey = valkey
        self._poll_interval = poll_interval
        self._members: dict[str, str] = {}  # queue key -> the ticket's last member there, maybe gone by now
        self._pubsub = valkey.pubsub()
        self._channels: set[str] = set()  # subscribed, named like the queues

    async def wait(self, timeout: float) -> None:
        """Wait for capacity to be freed in a quota the ticket was queued for, up to the timeout or poll interval.

        Returns at once after subscribing to new queues, as messages sent before are lost and the caller must retry.
        """
        timeout = min(timeout, self._poll_interval)
        try:
            if channels := self._members.keys() - self._channels:
                await self._pubsub.subscribe(*channels)
                self._channels |= channels
                return
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                if await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining) is not None:
                    return
        except ValkeyError:
            logger.warning("Failed to wait for quota messages, polling instead", exc_info=True)
            await asyncio.sleep(timeout)

    async def aclose(self) -> None:
        """Leave the queues, waking up the tickets queued behind, and stop listening for messages."""
        try:
            for queue_key, member in self._members.items():
                if await self._valkey.zrem(queue_key, member):
                    await self._valkey.publish(queue_key, "")
        except ValkeyError:
            logger.warning("Failed to leave quota queues, the tickets will expire", exc_info=True)
        finally:
            await self._pubsub.aclose()


def _hundredths(capacity: float) -> int:
    return round(capacity * 100)


async def _eval(valkey: Valkey, script: str, keys: Sequence[str], *args: object) -> Any:
    """Run a Lua script. Plain EVAL, as a server restart can empty the script cache."""
    # valkey-py annotates commands for its sync and async clients at once
    return await cast(Awaitable[Any], valkey.eval(script, len(keys), *keys, *map(str, args)))
