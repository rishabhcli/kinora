# `app/video/warmpool` — cold-start / warm-pool optimisation for video providers

Kinora generates film **a few seconds ahead of the reader**, so the latency of the
*first* request to a video provider — the auth handshake, TCP/TLS connection
setup, signed-session minting, connection-pool warmup — directly eats into the
lead time the scheduler has carefully built. This subsystem hides that
cold-start cost behind a per-provider pool of **warm, reusable sessions** that the
render path borrows and returns, kept "just warm enough" by a cost-aware pre-warm
scheduler driven by *predicted near-term demand*.

It manages **connections, not renders.** It never calls a provider's `render`,
never enqueues a job, and never reads `KINORA_LIVE_VIDEO`. Wiring it on cannot
spend a single video-second.

## Why a separate, local namespace

This is a FINAL-round subsystem: it cannot import the warm-pool/clock packages
from earlier rounds (they are not merged). So it defines its **own** local seams:

- `clock.Clock` — a `time()/monotonic()/async sleep()` time source. It mirrors the
  shape of `app.cache.clock` deliberately (familiar) but is *local* and adds the
  async `sleep` the pool's coroutines block on (fairness waiters, keep-alive loop).
  `VirtualClock` is a cooperative timer: sleepers park on per-deadline events and
  `advance()` fires every due timer in deadline order — fully deterministic.
- `protocols.SessionFactory` / `protocols.ProviderSession` — the only I/O seams.
  A session bundles whatever is expensive to create (auth token + connection +
  signed session). `factory.open()` is where cold-start latency lives; the pool
  times every `open` to learn each provider's cold-start cost. A real adapter
  wraps `app.providers.video_router.VideoBackend` and exposes it via `.handle`; the
  render path borrows a warm session and calls `handle.render(spec)` itself.
- `protocols.HealthSignal` — a read-only `available()` view of a provider's circuit.
  Rather than duplicate the breaker, the pool *reads* the existing
  `video_router.BackendHealth` (whose own method is `available()`) and drains when
  the circuit is open.

## The pieces

| Module | Responsibility |
|---|---|
| `clock.py` | `Clock` protocol + `SystemClock` + deterministic `VirtualClock`. |
| `protocols.py` | `SessionFactory`, `ProviderSession`, `HealthSignal` — injectable seams. |
| `cost.py` | `ColdStartModel`: measured first-vs-warm latency per provider; a conservative `planning_cold_s` (mean nudged toward the observed max) sizes the warm target; `worth_warming()` gates whether pre-warming pays off. |
| `demand.py` | `DemandModel`: EWMA of observed dispatch rate **max** the scheduler's look-ahead `hint`; `warm_target = ceil(rate × horizon)` clamped into `[floor, max_warm]` where `floor=min_warm` only if `worth_warming`. |
| `lease.py` | `Lease` (context-manager handle) + `FairWaiterQueue` (FIFO hand-off) + `LeaseTimeout` / `PoolDraining`. |
| `pool.py` | `ProviderPool`: min-warm maintenance, idle eviction, health-checked recycling, fair borrow/return with timeout + exhaustion back-pressure, circuit-aware drain. The **no-leak** invariant lives here. |
| `manager.py` | `WarmPoolManager`: owns the per-provider pools + demand models, runs the keep-alive / pre-warm loop, exposes the demand seams (`record_dispatch`, `hint`) and `borrow`. |
| `settings.py` | `WarmPoolConfig` (frozen dataclass) + `from_settings`. |

## The control loop (cost-aware pre-warm)

Every `keepalive_interval_s` the manager runs `tick()` per provider:

1. **Fold demand** — observed dispatches in the last window go into the demand EWMA;
   a window with no demand *decays* the rate so the target relaxes back to the floor
   after a burst clears (don't hold idle sessions for demand that's gone).
2. **Recompute target** — `DemandModel.warm_target(horizon, min_warm, max_warm,
   worth_warming)`. The scheduler's `hint` (reader velocity / buffer-watermark
   pressure — the §5.3/§4.3 seam) can lift the target *ahead* of the first render;
   a provider whose measured cold-start savings fall below
   `warm_worth_threshold_s` has its floor collapse to 0 (cost guard).
3. **Maintain** — the pool: drains if its circuit is open; otherwise evicts
   surplus idle sessions past `idle_ttl_s`, recycles stale (`max_session_age_s`) /
   unhealthy (`health_check_interval_s` probe) ones, and tops up to the target,
   bounded by `max_size`.

## Invariants

- **No warm-session leak.** Every opened session is always exactly one of: warm
  (idle, shelved), leased (out on a borrow), or closed. The pool serialises set
  mutations under one lock; a cold open reserves its `max_size` slot *before*
  awaiting the factory and releases it on failure; returns either hand off to a
  waiter, re-shelve, or close. Tested by reconciling live sessions against
  `warm + leased` after every scenario.
- **Fairness.** Borrowers blocked on an exhausted pool are served FIFO: a returning
  session is handed *directly* to the oldest live waiter, never re-shelved ahead of
  someone already blocked.
- **Bounded.** `max_size` caps total sessions (warm + leased); `max_warm` caps how
  many idle sessions a demand spike can provoke; `borrow_timeout_s` bounds the wait.
- **Circuit-aware.** An open provider circuit drains warm sessions and rejects new
  borrows with `PoolDraining`; parked waiters are failed (not hung). When the
  circuit recovers, the next tick re-warms.
- **Spend-safe.** The pool never renders; wiring it on cannot spend video-seconds.

## Settings (additive, OFF by default)

`warmpool_*` in `app/core/config.py` map onto `WarmPoolConfig.from_settings`.
`warmpool_enabled=False` (default) makes the manager inert: sessions open strictly
on demand (cold every time), no keep-alive loop — adopting the package changes
nothing until flipped on.

## Testing

`tests/test_video_warmpool.py` is fully deterministic: a `VirtualClock` drives
every timer and a `FakeFactory` simulates cold-start latency by advancing the
clock on `open`. No infra, no network, no real video, no spend. Covers min-warm
maintenance, idle eviction, health/age recycling, predictive pre-warm on a hint
spike, cold-vs-warm latency accounting → target, borrow/return + exhaustion
timeout, FIFO fairness, unhealthy-provider drain (and waiter failure + recovery),
open-failure tolerance, and the no-leak invariant after every scenario.
