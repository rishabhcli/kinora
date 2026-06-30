# Cost & Usage Analytics + Dashboards — `app/usageanalytics/`

A **time-series analytics warehouse over spend**. It rolls cost / usage / quality
events up by provider · model · book · session · time-bucket, detects anomalies
(spend spike, error surge, quality regression), projects budget burndown +
month-end spend against a `$30`-style cap, attributes cost
(`$/finished-minute-of-film`), and serves it all read-only to a dashboard UI.

## Why a separate subsystem (vs the three neighbours it sits beside)

* **`app/optim/cost_meter.py`** — a *single process-global accumulator* (one
  mutable rollup of "everything since boot", per model/op/book/session). It
  answers *"what has this process spent in total?"*. It has **no time axis**, no
  retention, no anomalies, no forecast. This subsystem is the time-series layer
  it lacks; it reads the same `Usage` shape and the same `PRICING` table so the
  USD numbers reconcile.
* **`app/finops/`** — *budget governance* over the budget-critical
  **video-seconds**: tiered caps, the promote/optimize/halt decision, the
  reading-trajectory forecast of forward seconds. It answers *"are we inside the
  cap right now, and what should we do?"*. This subsystem is the **money-side,
  historical, dashboard-shaped** complement — it projects USD over the calendar
  month and slices spend by every dimension, but it never makes a render
  decision.
* **`app/analytics/`** — the *product* event pipeline (reading behaviour,
  funnels, retention, cohorts). It answers *"how do humans use the product?"*.
  This subsystem answers *"what did the AI cost, how fast/reliably/well did it
  run, and is anything anomalous?"* — the operator/FinOps question.

## Module map

| Module | Responsibility |
|---|---|
| `events.py` | The `UsageEvent` fact (a superset of `providers.types.Usage` + dimensions + success/cache/quality), `Provider` inference, and `MetricCell` — the accumulating cell that holds running sums + bounded latency samples and exposes derived metrics (p50/p95, success/error/cache-hit rate, mean quality). PII-free. |
| `window.py` | `Granularity` (minute→month) floor/step/dense-bucket math; tumbling/sliding window generation; `RetentionPolicy` tiers + `downsample_buckets` (fold a fine grid into a coarse one via cell merge). |
| `store.py` | The `UsageMetricStore` protocol + the deterministic in-memory implementation (the default + test backend + viable embedded backend) and a redis-interface adapter seam. `Dimension` filtering + axis projection. |
| `aggregate.py` | The roll-up engine: dense `series`, `grouped` breakdowns, `leaderboard` top-N, `totals`, over a store. The `Metric` enum is the dashboard-selectable quantity. |
| `anomaly.py` | Spend-spike / error-surge / quality-regression detectors over a bucket series → `Alert`s with severity. Pure; thresholds in `DetectorConfig`. |
| `burndown.py` | Month-to-date USD burndown + run-rate + projected month-end vs the cap + ETA-to-cap + the per-day remaining-budget curve. Pure `Decimal` math. |
| `attribution.py` | Cost breakdown (per provider/model/book with shares) + `$/finished-minute-of-film` unit economics (a finished minute = 60 accepted video-seconds). |
| `service.py` | The `UsageAnalyticsService` façade the API reads through; owns the retention policy, detector config, and monthly cap; `from_settings` reads the additive `ua_*` settings. Infra-free. |

## API surface

`GET /api/usage-analytics/{overview,series,totals,leaderboard,anomalies,burndown,attribution,unit-economics}`
— all read-only, behind auth, and 404 when `usage_analytics_enabled` is off.
Registered additively in `app/api/routes/__init__.py`.

## Invariants

* **Never spends.** Read-only; no provider calls; `KINORA_LIVE_VIDEO` is
  irrelevant here.
* **Money is `Decimal`**, rendered as a *string* in JSON (no binary-float drift),
  matching `cost_meter`.
* **Pure analysis modules import no infrastructure** and never raise on ordinary
  input — degenerate inputs yield well-defined neutral results.
* The composition seam (`Container.usage_analytics_service()`) builds an in-memory
  store by default, so constructing it needs no DB/Redis.
