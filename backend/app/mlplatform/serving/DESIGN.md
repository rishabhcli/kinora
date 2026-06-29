# ML-platform serving facet (`backend/app/mlplatform/serving/`)

Facet **C** of the Kinora ML platform: the **model lifecycle + serving brain**. A
self-contained, *pure and offline* toolkit for the model-operations side of the
product — how Kinora's reasoning / judge / reward / draft models are versioned,
promoted, distilled, and served under load. The "GPU" is a discrete-event
**simulation**, never a device; there are **zero live model calls, zero credits**,
and `KINORA_LIVE_VIDEO` stays OFF throughout.

Cites: kinora.md §11 (model stack & budget accounting) and §13 (metrics / eval
harness). The serving simulator's read-ahead workload shape (long dwell, low
video-seconds consumed per wall-clock second) follows §4.1.

## The three-facet split (cross-facet contracts only)

The ML platform is built by sibling worktrees that share **only narrow, typed
`Protocol`s** — never direct imports:

- **Facet A — datasets.** Produces `Dataset` values (eval suites, distillation
  corpora). Consumed here through the `DatasetSource` protocol.
- **Facet B — reward model.** Scores candidate generations. Consumed through the
  `RewardModel` protocol for eval gates.
- **Facet C — distillation + model serving** (this package).

Because facets A/B may not be on disk yet, `contracts.py` defines the protocols
**and** deterministic offline fakes (`StaticDatasetSource`, `HeuristicRewardModel`,
`synthetic_dataset`) so this facet is fully usable and testable standalone. When the
real facets land, their concrete types structurally satisfy these protocols.

## Module map (all under `app/mlplatform/serving/`)

| Module | Responsibility | Status |
|---|---|---|
| `errors.py` | Package exception hierarchy (`MLPlatformError` + registry / distillation / serving subclasses). | ✅ |
| `contracts.py` | Facet-A/B `Protocol`s (`DatasetSource`, `RewardModel`), value objects (`Dataset`, `DatasetCase`, `RewardScore`), deterministic offline fakes + a seeded hash. | ✅ |
| `model.py` | Model value objects: `ModelKind`, the `Stage` promotion ladder, `ModelProfile` (serving characteristics for the simulator), immutable `ModelVersion` with lineage. Reuses the existing pure `llmops.semver`. | ✅ |
| `registry.py` | The model-lifecycle brain: append-only registry, lineage (`parent`/`teacher`) validation + walks, the `EvalGate` (consumes facets A+B), staged one-rung-at-a-time promotion, rollback, archive, audit log, `serving_set`. | ✅ |
| `distillation.py` | Knowledge distillation: teacher→student corpus generation (reward-filtered), a *simulated* saturating-curve training loop, compression-derived student `ModelProfile`, and student registration with `teacher=` lineage. | ✅ |
| `requests.py` | The serving workload: `InferenceRequest` (lifecycle + token math), `RequestState`, a fully-seeded `WorkloadGenerator` (Kinora read-ahead shape). | ✅ |
| `kvcache.py` | Paged KV-cache (PagedAttention model): fixed-size blocks, ceil-division sizing, reference-counted **content-addressed prefix reuse**, capacity/grow/free with the `used + free == capacity` invariant. | ✅ |
| `batching.py` | The **continuous-batching scheduler** under test: priority-then-FIFO admission, `max_batch_size` + `max_batch_tokens` (worst-case reservation) + KV-block + per-step admit limits, preemption + victim selection, runtime invariant guards. | ✅ |
| `speculative.py` | Speculative decoding: closed-form `E[accepted]` under geometric acceptance, speedup / cost-per-token model, and a seeded per-block draw that tracks the analytic mean. | ✅ |
| `metrics.py` | TTFT / end-to-end latency percentiles (nearest-rank), throughput (tok/s, req/s), cost, batch occupancy + KV-utilization + reuse + speculative speedup → a `ServingReport`. | ✅ |
| `simulator.py` | The **discrete-event serving simulator**: step-driven loop over the scheduler (admit → prefill → decode → grow/preempt → retire), idle-skip to next arrival, end-of-run invariant checks. Never mutates the caller's requests. | ✅ |
| `planner.py` | Capacity planner: sweeps a `SweepGrid` of serving configs, ranks by objective (throughput / p99 / cost / cost-per-token), and `recommend()`s the cheapest config meeting a p99 SLO. | ✅ |
| `catalog.py` | The default Kinora model catalog (brain / judge / reward / draft) with modeled serving profiles; `build_default_registry()`. | ✅ |
| `service.py` | `ServingPlatform` — the façade composing registry + distillation + simulator + planner into operator actions (distill → gate → promote/rollback → simulate / plan_capacity). | ✅ |

## The serving model (what the simulation captures)

- **Continuous (iteration-level) batching.** The batch re-forms every decode step:
  finished sequences leave, queued ones join, all subject to hard limits. The
  anti-starvation guarantee is priority-then-FIFO admission — a request can only be
  overtaken by a strictly higher-priority one.
- **Paged KV-cache + reuse.** Block-granular allocation (no external
  fragmentation). Content-addressed prefix blocks let requests sharing a prompt
  prefix (the Kinora canon-slice case) share physical blocks via reference counting
  — modeled end to end (the simulator reports the realized reuse ratio).
- **Speculative decoding.** A cheap draft proposes `k` tokens, the target verifies
  in one pass; the simulator commits `E[accepted]` tokens per step and charges the
  amortized cost, reporting the realized speedup.
- **Cost / latency / throughput.** All derived from the simulated timeline, so a
  capacity planner can compare configs without a GPU.

## Invariants (asserted in code **and** property-tested)

The brief requires the serving scheduler simulation to be property-tested for
invariants. These hold across a 40-seed sweep of randomized workloads + configs
(`test_mlplatform_simulator.py::test_property_*`, plus per-module sweeps):

1. **No request starves** — `n_completed == arrivals`; an oversized request (whose
   worst-case reservation exceeds the whole token budget) is admitted **solo** so it
   never waits forever.
2. **Batch-size limit respected** — `peak_batch_occupancy <= max_batch_size`.
3. **Token budget respected** — `reserved_token_total() <= max_batch_tokens` (the
   worst-case reservation dominates the live total, so the live total is bounded
   too); the sole exception is a single oversized request running solo.
4. **KV-cache never overcommitted and fully drains** — `used <= capacity` always,
   and `used == 0` at end of run (no block leak).
5. **Determinism** — same config + workload ⇒ bit-identical `ServingReport`.
6. **Token conservation** — total generated tokens == Σ each request's target.

## Tests (`tests/test_mlplatform_*.py`)

**294 pure/offline unit + property tests**, no infra, no network, no credits:
contracts, model, registry (lifecycle/lineage/gate/promote/rollback), distillation
(incl. full distill→gate→promote lifecycle), requests, kvcache (incl.
`used+free==capacity` sweep), speculative (analytic-vs-empirical sweep), batching
(invariant sweep), metrics, simulator (the named-invariant sweeps), planner, and the
`ServingPlatform` façade. Property tests use deterministic seeded sweeps (the repo's
existing pattern — no new dependency like Hypothesis).

## Shared-file additive changes

**None.** This facet is a brand-new package with **zero edits to any shared file**
(`core/config.py`, `composition.py`, `api/routes/__init__.py`, `db/models/__init__.py`
are untouched). It ships no DB tables and no API routes — it is a pure library +
façade. A future integration could add a lazy `Container.serving_platform` property
and/or a `/api/mlplatform` router; deliberately left out to keep the footprint
additive-only and the surface minimal. The only intra-repo dependency is a one-way,
read-only reuse of the existing pure `app.llmops.semver` helper.

## Roadmap / phases

1. Foundations — errors, cross-facet contracts + offline fakes. ✅
2. Model value objects + the promotion-ladder primitives. ✅
3. Model registry — versions, lineage, eval gate, staged promotion, rollback. ✅
4. Knowledge distillation — corpus generation + simulated training + lineage. ✅
5. Serving workload model + seeded generator. ✅
6. Paged KV-cache with content-addressed prefix reuse. ✅
7. Continuous-batching scheduler + invariants. ✅
8. Speculative decoding model. ✅
9. Discrete-event serving simulator + metrics. ✅
10. Capacity planner (config sweep + SLO recommendation). ✅
11. Default model catalog + `ServingPlatform` façade. ✅

### Future breadth (not yet built)
- A `python -m app.mlplatform.serving.run` CLI (sweep a workload, print a plan) mirroring `app.eval.run`.
- DB-backed `ModelRegistryStore` + an Alembic migration to persist versions/lineage/audit.
- Wire `ServingPlatform` into the composition root behind a lazy property + a gated `/api/mlplatform` router.
- Calibrate `ModelProfile` per-token times against measured DashScope/local kernels (still offline; just better constants).
- A multi-replica / tensor-parallel serving topology in the simulator (currently single-replica).
- Reward-weighted distillation (re-weight teacher examples by reward, not just filter).
