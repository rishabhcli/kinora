# DESIGN.md — Budget & Cost Governance (FinOps)

Living roadmap for the FinOps domain. Owner: the FinOps agent.
Cites `kinora.md` §11.1 (the budget reality + accounting system) and §4.4 (zones —
speculation is image-only, video-seconds are the scarce currency).

## Mission

Video-seconds are the hard-capped, scarce currency (~1,650s over the project's
lifetime, §11.1). The existing `BudgetService` (reserve/commit/release over an
append-only `budget_ledger`, advisory-lock-serialized) is the **load-bearing
guardrail** and MUST keep its contract — the Scheduler (`§4.6` promotion) and the
render pipeline (`§9.7`) both call it and must not change.

This domain builds a full **FinOps layer** *on top of* that contract:

1. **Multi-scope budgets** — tenant + per-session + per-scene + global, all as
   windowed sums over the same ledger, advisory-lock-serialized so two
   reservations cannot both slip past a cap.
2. **Cost forecasting** — project future video-seconds from a reading trajectory
   (velocity, remaining words, promotion rate) so the UI/Scheduler can see a
   burn-down and an ETA-to-exhaustion before it happens.
3. **Cost attribution** — per-agent and per-shot USD/physical attribution built
   on the existing `optim.cost_meter` `Usage` stream + the video ledger.
4. **Quality↔budget optimizer** — pick the render mode (full video / animatic /
   keyframe+KenBurns / text) that maximizes total quality under the remaining cap,
   given each mode's cost and expected quality.
5. **Tiered caps + alerts** — soft cap (warn), hard cap (the existing `BudgetExceeded`),
   floor (degrade), with per-scope tiered alert levels.
6. **Auditable cost ledger + reconciliation** — an append-only USD cost ledger
   (distinct from the physical video-seconds ledger) that can be reconciled against
   the video ledger and the cost meter, surfacing drift.
7. **Simulation harness** — a no-infra harness that drives synthetic reading
   sessions through forecasting + the optimizer to prove the system stays inside
   budget (KINORA_LIVE_VIDEO OFF, zero credits).

## Hard rules

- `KINORA_LIVE_VIDEO` stays OFF; zero credits. Everything here is accounting/sim.
- Never weaken the existing reserve/commit/release contract; only extend.
- Additive-only on shared files: `core/config.py` (new settings), `db/models/__init__.py`
  (new table exports), `composition.py` (new wiring). Documented below.
- New DB table => an Alembic migration chaining on the current head `a1b2c3d4e5f6`.

## Package layout — `backend/app/finops/`

- `tiers.py`          — budget scopes, tier thresholds, alert levels, `TieredCap` policy.
- `forecast.py`       — reading-trajectory cost forecasting + burn-down + exhaustion ETA.
- `attribution.py`    — per-agent / per-shot cost attribution over Usage + the video ledger.
- `optimizer.py`      — quality↔budget render-mode optimizer (greedy + knapsack-ish).
- `ledger.py`         — append-only USD cost ledger model wrapper + reconciliation.
- `governor.py`       — the orchestration facade: ties scopes+tiers+forecast+optimizer
                        into one budget-governance service the API/scheduler can read.
- `simulation.py`     — no-infra synthetic-session harness (proves we stay in budget).
- `service.py`        — `FinOpsService` aggregate exposed via the container.

DB: `backend/app/db/models/finops.py` — `cost_ledger`.
Repo: `backend/app/db/repositories/finops.py`.
Migration: `backend/migrations/versions/<rev>_finops_cost_ledger.py` (down=a1b2c3d4e5f6).

## Additive shared-file changes (documented)

- `core/config.py`: `finops_*` settings (tenant ceiling, soft-cap fraction, alert
  fractions, forecast horizon, optimizer mode prices/quality). Additive fields only.
- `db/models/__init__.py`: export `CostLedger`, `CostKind`. Additive.
- `composition.py`: build a `FinOpsService` lazily; expose `container.finops`. Additive.
- New API route `routes/finops.py` (own file) mounted in `routes/__init__.py` (additive).

## Milestones

- [x] M0 — Read §11.1/§4.4, study budget service/repo/scheduler/pipeline; green baseline.
- [x] M1 — `finops/tiers.py`: scopes + tiered caps + alert levels (pure, unit-tested).
- [x] M2 — `finops/forecast.py`: trajectory cost forecast + burn-down + exhaustion ETA.
- [x] M3 — `finops/attribution.py`: per-agent / per-shot attribution over Usage.
- [x] M4 — `finops/optimizer.py`: quality↔budget render-mode optimizer.
- [x] M5 — `finops/ledger.py` + DB `cost_ledger` model + repo + migration + reconciliation.
- [x] M6 — `finops/governor.py` + `service.py`: the governance facade; advisory-locked
           multi-scope reservation wrapping the existing BudgetService.
- [x] M7 — `finops/simulation.py`: no-infra synthetic-session harness.
- [x] M8 — API route `routes/finops.py` + composition wiring + config + tests green.
- [x] M9 — depth pass: EWMA `VelocityEstimator` (smooth noisy velocity for forecasts);
           `simulate_pool` (many tenants, ONE shared global ceiling — proves
           "no one drains the pool" under contention).

## Status / notes — DELIVERED

- New migration `c1d2e3f4a5b6` chains on head `a1b2c3d4e5f6`; verified upgrade+downgrade
  on a fresh DB and `alembic check` reports no `cost_ledger` drift (the 3 pre-existing
  vector/covering-index drifts are unrelated to FinOps).
- Tests: 72 new pure unit tests (tiers/forecast/optimizer/attribution/governor/ledger/
  sim + API-simulate) all green with no infra; 21 DB-gated tests (FinOpsService +
  gateway routes + the unchanged budget contract) pass on an isolated DB
  (`kinora_finops_test`) + redis db 15. Full suite: 1112 passed, 0 failed.
- Additive shared-file changes (all documented above): `core/config.py` (`finops_*`),
  `db/models/__init__.py` (`CostKind`/`CostLedger`), `db/repositories/__init__.py`
  (`CostLedgerRepo`), `db/repositories/budget.py` (added `used_seconds_for_books` —
  this file is in the FinOps domain), `composition.py` (`finops_policy` field +
  `build_finops`), `api/routes/__init__.py` (mount `finops.router`).
- Reserve/commit/release contract PRESERVED: `FinOpsService` wraps `BudgetService`
  unchanged; the tenant cap is an *additional* check under the same advisory lock.
- Pre-existing flaky test on main (NOT this domain): `test_scheduler_experiment.py::
  test_ab_deeper_buffer_is_at_least_as_smooth` — order/state-dependent; passes in the
  full-suite run. Left untouched (scheduler domain).
