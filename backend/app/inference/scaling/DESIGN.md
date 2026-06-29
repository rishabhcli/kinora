# `app.inference.scaling` — autoscaling + SLO routing brain (inference gateway, facet C)

> kinora.md §4 (generation-on-scroll), §11 (model stack & budget), §12 (engineering),
> §13 (eval harness). Read the cited sections before changing code that cites them.

The elasticity + SLO control plane for the inference gateway. It sits between the
**router** (facet A — the `app.providers`-backed request router and its per-backend
metrics) and the **GPU fleet**, and answers three operator questions, each proven by
a discrete-event simulation before it touches real cloud capacity:

1. **How much capacity to provision** — a *predictive* autoscaler (forecast +
   queue-theory), with scale-to-zero, a warm-pool floor, and anti-flap.
2. **Where to route under saturation** — an SLO-aware, cost-minimising backend
   selector, plus graceful load-shedding and priority preemption.
3. **What the cost↔latency trade is** — a Pareto-frontier sweep over the
   heterogeneous instance / warm-pool grid, assembled into a capacity-planning report.

Everything is **infra-free and deterministic**: pure models over injected clocks,
seeded RNGs, and conforming fakes. Zero Redis/Postgres/DashScope, zero model spend,
`KINORA_LIVE_VIDEO` stays off. The whole facet is unit-tested with no network.

## Why this is separate from the existing autoscaler / capacity code

The repo already has `app.queue.autoscale` (reactive render-*lane* sizing from queue
depth) and `app.reliability.capacity` (Little's-law / M/M/c capacity arithmetic) +
`app.reliability.slo` (SLO / error-budget / burn-rate math). This facet **reuses**
those primitives (`erlang_c`, `mmc_queue`, `SLO`, `SLOSet`, `LatencyDigest`,
`ReadingProfile`) rather than re-implementing them, and adds the layer none of them
cover: a *forecast-driven, heterogeneous-fleet, SLO-routing* brain validated by a
*discrete-event simulator*. The render-lane autoscaler sizes priority lanes from a
live queue; this sizes a GPU backend from a demand forecast against a cold-start
window — a different control problem.

## Module map

| Module | Responsibility |
|---|---|
| `contracts.py` | The `InferenceBackend` + `RouterMetricsSource` `Protocol`s facet C **consumes** from facet A (PEP 544, runtime-checkable — no import dependency), plus `BackendDescriptor` / `BackendTelemetry` and conforming fakes (`FakeBackend`, `FakeMetrics`). |
| `instances.py` | Heterogeneous GPU `InstanceType` catalog + the cost model: per-second / per-request billing, cold-start, spot reclaim hazard + survival, `CostBreakdown` (provisioned / cold-start / idle split). Default catalog: `gpu-l20` (cheap-slow), `gpu-a10` (workhorse), `gpu-h20` (dear-fast), `gpu-l20-spot` (cheapest, reclaimable). |
| `forecast.py` | Online demand forecasters: `EwmaForecaster` (level), `HoltForecaster` (level+trend ramp), `HoltWintersForecaster` (level+trend+diurnal seasonality), with online residual σ (Welford) → quantile headroom (`z_for_quantile`, Acklam inverse-normal). |
| `queueing.py` | SLO-aware sizing over the reliability M/M/c primitives: closed-form percentile wait `mmc_wait_quantile_s`, `servers_for_response_target` (mean), `servers_for_tail_target` (p95/p99), `size_fleet`. |
| `autoscaler.py` | `PredictiveAutoscaler`: forecast lookahead (size to the demand expected one cold-start ahead at a headroom quantile), scale-to-zero on sustained idle, warm-pool floor, asymmetric anti-flap (immediate up, cooldown-gated down) + `max_step` rate-limiting. Emits a `ScaleDecision` (desired count + rationale), never a side effect. |
| `controller.py` | `FleetAutoscaleController`: the ops entry point — manages several backends' autoscalers from one `RouterMetricsSource` (facet A's telemetry), derives each backend's demand from its snapshot, and emits a combined `FleetScalePlan` (desired count + delta per backend). The seam where facet C consumes facet A. |
| `pool.py` | `WorkerPool` / `Worker` state machine: `WARMING → WARM → BUSY → (DRAINING) → gone`, least-loaded slot scheduling, incremental cost accrual with cold-start / idle attribution, spot reclaim. |
| `workload.py` | Load profiles (`ConstantLoad`, `RampLoad`, `DiurnalLoad`, `BurstLoad`, `CompositeLoad`) + `ArrivalGenerator` (seeded non-homogeneous Poisson via thinning, committed/speculative tagging) + `reader_population_load` (ties §4.1 reader counts to arrival rate). |
| `routing.py` | `SLORouter`: drop unhealthy / penalise degraded, then cheapest-within-SLO (speculative) or fastest-within-SLO (committed), with a fastest-backend rescue when nothing meets the budget. Projects per-backend tail latency from live telemetry. |
| `shedding.py` | `LoadShedder`: priority-aware admission — committed never shed (buffer is sacred, §4.4), speculative shed with a load-proportional probability past the saturation knee (the §4.4 Ken-Burns degradation ladder), plus a hard global queue cap. |
| `preemption.py` | `PreemptionPlanner`: a committed arrival onto a full fleet preempts the *youngest* eligible speculative victim (least wasted compute), respecting progress + min-remaining guards (cooperative cancel, §4.8/§12.1). |
| `simulator.py` | `FleetSimulator`: the discrete-event engine wiring pool + autoscaler + shedder + preemption against a workload, producing a `SimulationResult` (SLO attainment, latency percentiles, cost breakdown, preemptions, reclaims, wasted compute). Pure fn of (config, seed). |
| `pareto.py` | `ParetoSweep` / `ParetoFrontier`: simulate a candidate grid (instance × warm-pool), keep the feasible non-dominated set on (cost↓, latency↓), expose cheapest / fastest / knee selectors. |
| `reports.py` | `CapacityPlanner` → `CapacityReport`: analytical sizing + validation sim + SLO verdict (reusing `app.reliability.slo.SLO`) + Pareto frontier + the recommended (knee) config; `render_text()` for a CLI, `to_dict()` for a dashboard. |

## Key design decisions

- **Consumed protocols, not inheritance.** Facet A's router classes satisfy
  `InferenceBackend` / `RouterMetricsSource` *structurally*, so the two facets ship
  and merge independently. This package ships its own conforming fakes so it is fully
  testable before facet A's `app/inference/router/` lands (currently an empty slot).
- **Forecast then size.** The autoscaler scales on `forecast.quantile(p95)` of the
  demand one cold-start ahead, then sizes that demand with the tail-latency queue
  solver — capacity arrives *before* the load, not one cold-start late.
- **Scale-to-zero is a single deliberate step.** While idle-but-not-past-the-window,
  the pool *holds* capacity (a returning reader is served warm); the collapse to zero
  is one `TO_ZERO` step once the window elapses, never a slow bleed.
- **Priority is the §4.4 zone order.** Committed (full video the reader is arriving
  at) is never shed and may preempt; speculative (keyframe prefetch) degrades to the
  Ken-Burns ladder — so shedding speculative is the *designed* graceful path, not a
  failure.
- **Proof by simulation.** Every sizing/routing/Pareto claim is validated by the DES
  under varied load (constant / ramp / diurnal / burst / reader-population) and graded
  with the same `SLOSet` machinery as the rest of Kinora's reliability gating.

## Additive changes to shared files

This facet is otherwise **fully self-contained** under `backend/app/inference/`
(new package) + `backend/tests/inference/` (new tests). The only touch outside the
package:

- `backend/app/api/routes/__init__.py` — a single import-ordering normalisation
  (`films`/`finops` reordered alphabetically) to satisfy ruff `I001`. Pure
  cosmetic reordering, no semantic change; merge-safe. Made only because the shared
  file had drifted out of import order from concurrent additive router imports by
  sibling agents.

No models, migrations, config keys, or composition-root wiring were added — the
facet is a pure library consumed by facet A / an ops CLI / a capacity dashboard.

## Tests

`backend/tests/inference/scaling/` — 175 tests, all infra-free and deterministic:
`test_contracts`, `test_instances`, `test_forecast`, `test_queueing`,
`test_autoscaler`, `test_pool`, `test_workload`, `test_routing`, `test_shedding`,
`test_preemption`, `test_simulator`, `test_pareto`, `test_reports`,
`test_integration`. Run: `backend/.venv/bin/pytest tests/inference -q`.
