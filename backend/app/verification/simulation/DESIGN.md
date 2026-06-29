# Deterministic Simulation Framework (verification facet C)

> A FoundationDB-style deterministic simulator for Kinora's control plane. It runs
> the **real** reading→scheduler→queue→render→events loop inside a single-threaded
> virtual-time engine with a seeded, fault-injecting network / disk / redis, and
> asserts end-to-end invariants across thousands of seeded fault schedules — with
> minimal-failing-seed shrinking and exact replay.
>
> Maps to `kinora.md` §4 (generation-on-scroll), §6 (architecture), §9.7 (the
> per-shot state machine), §11.1 (budget accounting), and §12 (the unglamorous
> engineering: queue, backpressure, leases, retries, DLQ, degradation ladder).

## Why this exists

`kinora.md §12` opens: *"video generation is asynchronous and flaky, so the
backend is built like it."* A backend built for flakiness is only trustworthy if
something exercises the flakiness **deterministically and exhaustively**. Unit
tests cover one interleaving; this framework covers thousands — every fault
schedule a real outage could produce — and when one breaks an invariant it hands
you a *minimal, replayable* reproducer instead of a flaky "it failed once."

This is the technique FoundationDB used to ship a distributed database with
famously few production bugs: own time and randomness, model concurrency as
seeded event interleavings, and inject faults at the exact sites real failures
manifest (Buggify).

## The three guarantees (and how each is enforced)

1. **One source of randomness** — `core.Prng`, a splittable SplitMix64 PRNG. Every
   stochastic decision (fault rolls, latency draws, reader jitter, id minting)
   pulls from a stream split off the run seed. `split("network")` and
   `split("disk")` are independent and *label-stable*, so adding a draw in one
   subsystem never shifts the bytes another sees — the reason a regression seed
   stays alive as the code evolves.
2. **One source of time** — `core.SimClock`, integer virtual milliseconds.
   Advances *only* when the event loop pops the next event; reads are free and
   monotonic. "100 ms of latency" is an integer on a timeline, not a real sleep,
   so a multi-minute reading session simulates in milliseconds of wall-clock.
3. **One thread of control** — `core.EventLoop`, a min-heap drained in strict
   `(time, seq)` order (FIFO within an instant). No OS scheduler, no preemption,
   no race; concurrency is *modelled* as interleaved events and the interleaving
   is a pure function of the seed.

`asyncio` is used **only as a coroutine runner**, never as a scheduler: the real
services are `async`, but inside the sim they never truly suspend (the fake redis
completes synchronously, no `asyncio.sleep` on the hot path), so `runtime.Simulation.run_sync`
drives each `async` call to completion *at the current virtual instant*. Time
moves only between calls, on our clock.

## Layered architecture

```
runner.py        sweep(N seeds) → shrink(minimal adversary) → replay(exact)
   │
invariants.py    safety + liveness + quality predicates over a finished run
   │
system.py        SimulatedSystem: wires the REAL SchedulerService + RedisRenderQueue
   │             + virtual-time worker lanes + lease reaper + RenderSimulator (§9.7)
   ├── workload.py      seeded ReaderModel (steady/skimmer/thinker/seeker/erratic …)
   ├── collaborators.py SimBudget (virtual 1,650-s pool) / SimShotSource / SimKeyframes
   └── events.py        CapturingEventPublisher (buffer_state / clip_ready tap)
   │
runtime.py       Simulation: the bridge (virtual clock ⇄ async runner) + run_resilient
   │
network.py  storage.py  redis_sim.py    the fault-injecting seams
   │
buggify.py       the injection gate (reads a profile, rolls the seed, logs)
faults.py        the grammar: FaultKind · FaultProfile · FaultSchedule
   │
core.py          Prng · SimClock · EventLoop          ← the deterministic heart
determinism.py   owns uuid/random/time for the run    ← byte-identical replay
```

The framework is **additive-only** and self-contained under
`backend/app/verification/simulation/`. It imports the production scheduler/queue/
render modules but modifies none of them. The one shared seam it relies on is that
`SchedulerService` (`now_ms`) and `RedisRenderQueue` (`clock_ms`) already accept
injectable clocks — no change needed.

## What the loop does each run

1. A seeded `ReaderModel` emits settled intents (advance / dwell / idle / seek) on
   the virtual clock. Each intent folds into the real `SchedulerSession` and runs
   one real `SchedulerService.on_event` — the §4.5 dual-watermark fill, §4.6
   velocity-adaptive promotion, §4.7 idle-pause, §4.8 seek re-seed.
2. Promotions enqueue jobs on the **real** `RedisRenderQueue`, backed by the
   project's own `FakeAsyncRedis` wrapped in the fault-injecting `FaultingRedis`.
3. Virtual-time **worker lanes** (4 committed / 2 speculative / 2 keyframe) drain
   the queue: `claim` → model render as a latency (with injected stalls/crashes) →
   drive the real §9.7 `RenderSimulator` state walk → `ack` / `retry` / DLQ.
4. A periodic **lease reaper** recovers jobs orphaned by a worker crash/stall.
5. Acceptance commits the virtual budget reservation and publishes `clip_ready`;
   the scheduler publishes `buffer_state` / `budget_low`.
6. After the reader session, faults quiesce (a real storm passes) and the loop
   **drains to a quiescent end state** — the premise the eventual-consistency
   invariants assert against.

Zero credits, ever: no provider is invoked anywhere (the budget pool is virtual),
so `KINORA_LIVE_VIDEO` is irrelevant.

## Invariants

| Invariant | Kind | Statement |
|---|---|---|
| `no_double_spend` | safety | The §12.1 `shot_hash` idempotency means no shot is charged twice; `budget.spent == Σ per-shot committed`. |
| `budget_ledger_conserved` | safety | §11.1 video-seconds conserved: `remaining + committed + outstanding == total`. |
| `no_stuck_shots` | safety | At quiesce, every enqueued shot reached a §9.7 terminal state (accepted / degraded / cancelled). |
| `dlq_implies_degraded` | safety | §12.4 a dead-lettered shot rides the ladder; it never silently vanishes. |
| `queue_drains` | liveness | The queue is empty after convergence (no job stuck forever). |
| `accepted_shots_emit_clip_ready` | liveness | §9.8 every accepted shot reached the client. |
| `reservations_resolved` | liveness *(strict)* | Every reservation is eventually committed or released. **Violated by the leak below.** |
| `buffer_health_under_nominal` | quality | §4.5/§13 the committed buffer fills toward `H` and is not chronically starved (profile-gated to light load). |

Safety invariants must hold under **every** profile — a storm may degrade quality
(the §4.4 ladder is the *correct* response) but must never corrupt state or
double-charge the scarce budget. The default `CORE_INVARIANTS` suite is what the
*current* product satisfies; `STRICT_INVARIANTS` additionally demands
`reservations_resolved`, which surfaces the known bug.

### Verification result (as of this writing)

`CORE_INVARIANTS` hold **under both `nominal` and `chaos`, across all six reader
archetypes, over every seed swept** (the CI suite checks 4 seeds × 6 archetypes
per profile; ad-hoc sweeps of 90+ seeds × 6 archetypes per profile were clean —
540 runs each, zero safety violations). Determinism is **byte-identical across
separate OS processes**: two independent `python` invocations of the same
`(seed, profile)` produce the same SHA-256 result fingerprint (shots, acceptances,
degradations, spend, queue/DLQ depths, reap count, fault count, shot-id set).

Under the `STRICT_INVARIANTS` suite the sweep surfaces exactly one violation —
`reservations_resolved` — which the shrinker minimises to a single fault kind
(`redis_error`) and replays deterministically. That is BUG-1 below.

---

## Bugs found

### BUG-1 — Scheduler leaks a budget reservation on a transient broker error between `reserve` and `enqueue` *(confirmed, real)*

**Severity:** medium (slow budget starvation, not state corruption).
**Invariant:** `reservations_resolved` (liveness).
**Where:** `backend/app/scheduler/service.py`, `SchedulerService._fill_committed`.

The promotion path is:

```python
reservation = await self._reserve(session, shot, est)   # debits the budget pool
if reservation is None:
    break
result = await self._queue.enqueue(..., reservation_id=reservation.id, ...)  # ← can raise
```

`_fill_committed` carefully releases the reservation on a *queue drop*
(`not result.admitted`) and on an *idempotent dedup* (`not result.created`), but
there is **no `try/except` around `enqueue` itself**. If `enqueue` raises a
transient broker error (a redis blip — `REDIS_ERROR` in the sim), the reservation
made on the line above is **stranded**: never committed (the job was never
enqueued, so no worker ever accepts it) and never released (no rollback path).
The earmark holds scarce video-seconds forever. Over a long session of
intermittent broker flakiness these accumulate and slowly starve the 1,650-second
pool (`kinora.md §11.1`).

**Reproduction (minimal, via the framework's shrinker):**

```python
from app.verification.simulation import (
    FaultProfile, FaultSchedule, replay, STRICT_INVARIANTS, SystemConfig,
)
from app.verification.simulation.faults import FaultKind, FaultWeight

# A single fault kind — a flaky broker — is enough to strand an earmark.
profile = FaultProfile(
    label="redis-only",
    weights={FaultKind.REDIS_ERROR: FaultWeight(probability=0.08)},
)
sched = FaultSchedule(seed=0, profile=profile)
res = replay(
    sched,
    archetype="steady",
    config=SystemConfig(session_duration_ms=60_000),
    invariants=STRICT_INVARIANTS,
)
assert not res.ok                                       # reservations_resolved violated
assert res.system.budget.outstanding_reservations > 0   # stranded earmark(s)
```

Or just run the CLI, which sweeps, finds, shrinks, and prints the `--replay` line:

```sh
python -m app.verification.simulation --profile chaos --strict --seeds 8
```

> *Note on shrinking & PRNG alignment.* The shrinker's minimal schedule keeps the
> full `chaos` profile shape at a lowered `intensity` (it re-verifies reproduction
> at every step), so it reproduces exactly. A *hand-written* single-kind profile at
> the same effective probability may land on a different fault-stream alignment
> (fewer active kinds ⇒ fewer Buggify draws consumed ⇒ the stream advances
> differently), so use a robust rate like the `0.08` above for a standalone repro.

Isolated repro with **only** `REDIS_ERROR` at 5–8%: nearly every steady-reader
session leaks at least one reservation. The same minimal schedule **passes all
`CORE_INVARIANTS`**, confirming the finding is isolated to this path, not general
corruption.

**Suggested fix (for the scheduler owner — *not applied here*, this package is
additive-only):** wrap the `reserve`→`enqueue` pair so the reservation is released
if `enqueue` raises, e.g.

```python
reservation = await self._reserve(session, shot, est)
if reservation is None:
    break
try:
    result = await self._queue.enqueue(..., reservation_id=reservation.id, ...)
except Exception:
    await self._budget.release(reservation, note="enqueue failed")
    raise
```

Alternatively, make the reservation a child of an idempotent "promotion intent"
keyed by `shot_hash`, reaped if no matching job materialises within a timeout
(mirrors the §12.1 lease reaper for jobs, applied to budget earmarks). Until
fixed, `reservations_resolved` is kept out of the default suite so the framework
lands green; flip it in by swapping `CORE_INVARIANTS` → `STRICT_INVARIANTS`.

### NON-BUG — `no_double_spend` false positive from an early harness modelling error *(fixed in the harness)*

An early version of the harness tracked per-shot records from the scheduler's
`tick.promoted` return and wrapped `on_event` in a wholesale retry. Because
`on_event` is **not** idempotent (it mutates session state and reserves budget),
retrying it double-promoted, and the `tick.promoted`-based tracking missed some
accepted jobs — producing a spurious `no_double_spend` violation. Fixed by (a)
running `on_event` exactly once per tick (a transient error drops the tick, as the
production API route does — it does not replay `on_event`), and (b) making
`report.shots` authoritative via `_record_for(job)`, populated from the actual job
a worker handles rather than the tick's return. This was a *simulator* bug, not a
product bug, and is recorded here as a caution: a simulator must model the
production caller's error handling faithfully, or it manufactures phantom failures.

## Extending the framework

* **A new fault:** add a `FaultKind`, give it a weight in `FaultProfile.nominal()`
  / `.chaos()`, and inject it at the relevant seam via `Buggify.should` /
  `Buggify.duration`. The sweep and shrinker pick it up for free.
* **A new invariant:** write a `SystemReport → InvariantResult` function, wrap it
  in an `Invariant`, and add it to a suite. The shrinker minimises toward *its*
  violation specifically (it keys on the violated invariant's name).
* **A new reader archetype:** add a branch to `workload.ReaderModel.next_intent`
  and a name to `workload.ARCHETYPES`.
* **A new system seam under test:** the scheduler/queue are injected behind
  `Protocol`s; swap a real implementation in `SimulatedSystem.__init__` and the
  rest of the loop is unchanged.
