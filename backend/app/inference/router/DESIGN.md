# Inference request router (`backend/app/inference/router/`)

**Facet A of the Kinora inference gateway** — a high-throughput LLM-inference
*scheduling brain* over the existing provider + resilience layer
(`app/providers/`, `app/providers/resilience/`). Where that gateway makes a
**single call** resilient (timeouts, circuit breaker, token-bucket + adaptive
rate limit, per-call dedup, hedging), this layer decides — at throughput —
*which* of many concurrent requests run *where* and *when*:

- **continuous / in-flight batching** with token-budget bin-packing,
- **priority + weighted-fair-share** scheduling across tenants/agents,
- **admission control + backpressure + queue-time SLAs**,
- **KV-cache-affinity routing** (same-prefix requests → same worker),
- **request coalescing** (identical in-flight requests pay once),
- a clean **`InferenceBackend` protocol** + cross-facet seams,
- a **deterministic simulator** validating fairness + throughput.

It composes *around* the round-1/round-2 provider code — it never edits it. A
backend (`ChatProviderBackend`) funnels each real call through the existing
`ChatProvider` (and, when wired, the `ResilientGateway`), so per-call resilience
and cross-request scheduling stack cleanly.

Cites **kinora.md §11** (model stack / budget accounting) and **§12** (render
queue, concurrency/backpressure/preemption, caching layers, observability).

## Design constraints (per the worktree brief)
- **Own NEW package only** — everything lives under `app/inference/router/`.
- **Additive-only** on shared files, documented below.
- Every model call is behind an injected **`InferenceBackend`**; the bundled
  defaults (`EchoBackend`, `SimBackend`) are **deterministic fakes** — zero live
  calls, zero credits. `KINORA_LIVE_VIDEO` stays OFF.
- **Deterministic**: clock + RNG are injectable, so fairness/throughput is
  asserted in unit tests with no wall-clock waiting.

## Module map (all under `app/inference/router/`)

| Module | Responsibility |
|---|---|
| `errors.py` | Package exception hierarchy (`RouterError` + subclasses: admission/SLA/config/backend). |
| `request.py` | `InferenceRequest` (content-free schedulable unit), `RequestPriority` (4 classes), `RequestState`, `prefix_key_for`. |
| `protocols.py` | **Cross-facet seams**: `InferenceBackend`, `InferenceResult`, `PrefixCacheOracle` (facet B), `WorkerPoolController` + `WorkerView` (facet C), `Clock`. |
| `worker.py` | `Worker` (KV token budget + slot count + resident-prefix LRU) and `WorkerPool` (per-model; implements `WorkerPoolController`). |
| `fairshare.py` | Strict-priority over per-flow **virtual-time WFQ**; `select_next`/`commit` so fairness accounting tracks *genuine* dispatch. |
| `binpack.py` | `TokenBinPacker` — order-preserving token + slot + chunked-prefill bin-packing; surfaces oversized requests. |
| `affinity.py` | `AffinityRouter` — scores workers by warm-prefix overlap vs. load; `ResidencyOracle` is the default `PrefixCacheOracle`. |
| `coalescing.py` | `CoalescingTable` — leader/follower in-flight dedup; fans one result out to all waiters. |
| `admission.py` | `AdmissionController` (hard ceiling, soft-zone low-priority shedding, per-tenant caps, unservable reject) + `QueueTimeSLA`. |
| `cancellation.py` | `CancellationToken` + `CancellationRegistry` — cooperative abort tied to a session/trajectory (§4.8 / §12.1). |
| `metrics.py` | `RouterStats` counters + `P2Quantile` (streaming p50/p99 wait, O(1) memory) → snapshot for the SLO controller / §12.5 panel. |
| `router.py` | **`InferenceRouter`** — the composition root; `submit → tick → complete` loop wiring every primitive above. One router per model. |
| `dispatcher.py` | `MultiModelRouter` — thin façade routing by `request.model` to per-model routers; fan-out tick/cancel; aggregated stats. |
| `planner.py` | `BatchPlanner` — offline batch planning over the bin-packer (the §11.1 batch-API ~50%-off lane: Phase-A analysis, bulk keyframes, re-scoring). |
| `backends.py` | `EchoBackend` (network-free default) + `ChatProviderBackend` (composes with `app.providers.chat`). |
| `factory.py` | `build_router` / `build_multi_model_router` — one-line wired, off-gate-safe constructors for the composition root. |
| `simulator.py` | `RouterSimulator` — seeded discrete-event harness over a `VirtualClock` + `SimBackend`; validates fairness + throughput. |

## The scheduling loop (`router.py`)

```
submit(request)
  └─ coalescing       §12.3 dedup — a follower awaits the leader, never scheduled
       └─ admission   §12.2 backpressure — shed low-priority / cap tenant / reject, else enqueue
            └─ fair-share queue   §12.2 strict priority over per-flow WFQ

tick()  (driven by caller / event loop / simulator)
  └─ drop SLA-expired heads (§12.2)
  └─ capacity-aware WFQ: repeatedly select_next() the highest-priority,
        lowest-virtual-time request whose head can be PLACED on some worker;
        place it on the KV-warmest fitting worker (affinity + load balance);
        commit() it (advances that flow's virtual time + served cost);
        skip a flow whose head fits no worker this tick (no mis-charge, no stall)
  └─ execute_batch() on the InferenceBackend per worker
       └─ settle the leader + fan out to coalesced followers; release capacity;
          a tripped cancellation token discards the result as CANCELLED
```

**Why `select_next`/`commit` (not a plain `pop`)**: the router places by
capacity, so a peeked request may not be dispatched this tick. Charging WFQ
virtual time only on `commit` (genuine dispatch) is what makes weighted fair
share actually hold — an earlier draft that popped-then-re-enqueued mis-charged
every peek and flattened the weight ratio to 1:1. The simulator's
`test_weighted_fair_share_holds_under_load` is the regression guard (a weight-3
tenant gets ~3× the weight-1 tenant's served work, measured mid-backlog).

**Concurrency model**: a single-writer scheduling loop. `submit` only touches
the queue + coalescing table under one lock; `tick` forms batches under the lock
then awaits backends. One router per worker process; cross-loop use is out of
scope.

## Shared protocols — the seam between the three gateway facets

The inference gateway is built by three sibling facets that meet **only** at the
`runtime_checkable` Protocols in `protocols.py`. Each facet ships and tests
independently against deterministic fakes; the router (A) never imports B or C.

### `InferenceBackend` (A → transport / B)
```python
class InferenceBackend(Protocol):
    @property
    def model(self) -> str: ...
    async def execute_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResult]: ...
```
Executes a co-scheduled micro-batch on one worker/engine. Reports a per-request
failure as `InferenceResult(error=...)` (never raises for one request); may raise
`BackendError` for a whole-batch fault. **Facet B (accel)** plugs in here as a
*decorator*: a speculative-decoding / response-cache wrapper that wraps a base
backend, reports `cache_hit` / `accepted_tokens` on the result, and is otherwise
transparent to the router.

### `PrefixCacheOracle` (A ← B)
```python
class PrefixCacheOracle(Protocol):
    def warm_fraction(self, prefix_key: str | None, worker_id: str) -> float: ...  # [0,1]
```
The affinity router biases routing toward the warmest worker via this signal.
The bundled `ResidencyOracle` returns exact 0/1 membership from each worker's
resident-prefix LRU; **facet B** can swap in a smarter (radix-tree / fractional)
oracle without touching the router.

### `WorkerPoolController` + `WorkerView` (A ← C)
```python
class WorkerPoolController(Protocol):
    def snapshot(self) -> list[WorkerView]: ...
    def add_worker(self, worker_id: str) -> None: ...
    def drain_worker(self, worker_id: str) -> None: ...
```
`WorkerPool` implements this. **Facet C (scaling)** reads `snapshot()` +
`RouterStats.snapshot()` (utilization, queue depth, p99 wait, reject rate) and
calls `add_worker` / `drain_worker` to react to load + SLO — without reaching
into router internals. `WorkerView` exposes only capacity/load/health.

### `RouterStats.snapshot()` (A → C / §12.5 panel)
A flat JSON-safe dict: admitted/rejected/expired/coalesced/dispatched/
succeeded/failed/cancelled, avg batch size, tokens in/out, **wait p50/p99**
(streaming P² estimator, O(1) memory), reject rate, cache-hit rate, rejects by
reason, served by priority. This is the SLO controller's input and the live
metrics-panel feed.

### Suggested division of labour with the siblings
- **Facet A (this package)** owns: queue ordering, fair share, admission,
  bin-packing, affinity *routing decision*, coalescing, the per-model loop.
- **Facet B (accel)** owns: an `InferenceBackend` decorator implementing
  speculative decoding + a prefix/response cache, and a smarter
  `PrefixCacheOracle`. It reports `cache_hit` / `accepted_tokens` back so A's
  stats stay honest.
- **Facet C (scaling)** owns: an autoscaler + SLO controller driving
  `WorkerPoolController` from `RouterStats` + `WorkerView`, plus replica-level
  load balancing across multiple `InferenceRouter` instances of the same model.

## Additive shared-file changes
The router is import-isolated and wires in lazily; the composition change is
strictly additive (a new optional accessor on the container, off-gate-safe
because the default backend is the network-free `EchoBackend`). No existing
symbol, signature, table, or route is modified. See the composition section of
the worktree summary for the exact additive lines.

## Test inventory (`tests/test_inference_router_*.py`)
`request`, `fairshare`, `binpack`, `worker`, `affinity`, `coalescing`,
`admission`, `metrics`, `cancellation`, `router` (integration), `dispatcher`,
`planner`, `backends`, and `simulator` (the fairness/throughput proof). All run
with no infra and no network.
