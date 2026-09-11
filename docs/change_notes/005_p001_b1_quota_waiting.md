# 005 Quota waiting in arrival order (B1)

Implements finding B1 of `docs/proposals/001_review_by_qwen_flash.md`.

## Why

Requests waiting for a busy quota re-walked their routes every 0.2 s, and whoever retried first after capacity
freed up got it. A request could wait until its 429 while newer requests of the same key were served. B1 was
first rejected in `003_p001_review_by_qwen_flash.md`, as none of the options it proposed made waiting fair.

Quota budgets were floats, and the acquire script needed `MIN_BUDGET - 1e-9` so that sums like `4.1 - 4`
would not reject a whole `0.1`.

## How

- **Arrival order.** Each quota gets a queue next to its leases: a Valkey sorted set of tickets, with
  `<arrival time in microseconds>|<ticket id>` members scored by expiry. The acquire script sets a whole unit
  aside for every ticket that arrived earlier, then takes up to 1 unit of what is left, or else queues or
  refreshes the request's ticket. Freed capacity goes to the oldest ticket, and capacity beyond what the
  tickets can take stays free for anyone.
- **Ticket lifetime.** A ticket leaves a queue when it gets a lease there, and all tickets of a request leave
  once it is routed. A ticket expires 3 poll intervals after its last retry, so a crashed request, or one busy
  trying another route, doesn't hold capacity back for long.
- **Pub/sub wake-up.** Removing a lease, or a ticket leaving a queue, publishes on the queue's channel. A
  waiting request subscribes to the channels of its queues and retries at once after subscribing, as messages
  sent before are lost. It still retries at least every poll interval (1 s), since expired leases and tickets
  send no message, and falls back to polling when pub/sub fails.
- **No wake-up loops.** A lease not used by any response is removed without a message. Otherwise requests
  waiting for nested quotas, taking and dropping the outer lease on every retry, would wake each other up
  endlessly.
- **Integer hundredths.** Budgets are stored and summed as integer hundredths, and config requires
  `max_concurrency` to be a multiple of `0.01`.

## What other options were considered

- **Jitter on the poll interval**: not chosen. It keeps waiters from polling in lockstep, but has little
  effect at 1-20 RPS and doesn't make waiting fair.

## What was implemented

- `llm_gateway/quota.py`: acquire script with the ticket queue and hundredths, `Ticket` (`wait`, `aclose`),
  `QuotaStore.ticket()` and `poll_interval`, `Lease.release(wake_waiters=...)` publishing on removal.
- `llm_gateway/routing.py`: `ModelRouter.open` waits with a ticket and closes it once routed; unused leases
  are released without waking waiters; `poll_interval` moved to `QuotaStore`.
- `llm_gateway/config.py`: `max_concurrency` must be a multiple of `0.01`.
- `tests/`: 8 new test cases (arrival order, leaving a queue, capacity beyond the queue, expiry of a gone
  request's ticket, wake-up messages, exact hundredths, arrival order through the app, config check). The
  app tests poll less often than their quota timeout, so they pass only through wake-up messages. 79 tests
  pass 5 runs in a row; `uv run ty check` passes.
- `docs/wiki/architecture.md` (new Waiting section), `docs/wiki/configuration.md`.

Old and new replicas count budgets differently, so they should not share Valkey during a deploy.
