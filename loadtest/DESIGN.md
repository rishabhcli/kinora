# Kinora Reliability Toolkit — DESIGN.md (living roadmap)

> Domain: **Reliability engineering** — load / chaos / synthetic monitoring /
> capacity planning / runbooks-as-code for the generation-on-scroll backend
> (kinora.md §4, §12).
>
> Two homes:
> * `backend/app/reliability/` — the reusable, **unit-tested** models, math, and
>   probes (pure given their collaborators; zero infra, zero model spend).
> * `loadtest/` — the **CLI runner** + scenario library that drives the models
>   against an *explicitly-provided* target URL. Tests use a fake transport;
>   **no real load in tests**.

## Why this exists

kinora.md sells two engineering claims a judge will probe: generation-on-scroll
stays *smooth and not-always-generating* (§4.5 watermark hysteresis), and the
queue is *idempotent, cancellable, backpressured, dead-lettered* (§12.1–§12.2).
Reliability engineering turns those slogans into **measured properties**: we
model realistic reader traffic, inject failures at the provider/redis/db seams,
probe critical journeys, and size capacity with queueing math so the
1,650-second budget and the worker pool are provably adequate (or provably not).

## Design constraints (hard rules from the brief)

1. **Additive-only on shared files.** `core/config.py` gains a block of *new*
   fields with defaults; nothing existing is edited. Documented below.
2. **No real load in tests.** The load runner executes only against an
   explicitly-provided `--target` URL via the CLI. Unit tests drive a **fake
   transport** + deterministic clocks and RNG. `KINORA_LIVE_VIDEO` stays OFF;
   zero credits.
3. **Deterministic.** Every model takes an injected clock and `random.Random`
   seed so the scenario/analysis/capacity logic is reproducible and unit-testable.
4. **Stay in our lane.** Only `loadtest/` and `backend/app/reliability/` (+ the
   additive config block). Nine other agents work in parallel; we never edit
   their domains.

## Package map — `backend/app/reliability/`

| Module | Responsibility | kinora.md |
|---|---|---|
| `latency.py` | Streaming latency digest: p50/p90/p99/p999, min/max/mean, count, HdrHistogram-style log-bucket histogram, mergeable across workers | §12.5 |
| `metrics_report.py` | Per-endpoint + aggregate report: throughput (req/s), error rate, latency percentiles, status breakdown; text + JSON render; mergeable | §12.5, §13 |
| `reader_model.py` | The §4.3/§4.7 reader as a state machine: READING / SKIMMING / SEEKING / IDLE; emits the request stream a real client would (intent/seek/idle) | §4.3, §4.7, §4.8 |
| `workload.py` | Open (Poisson arrivals via thinning under a ramp) and closed (N looping users, think-time) models + ramp profiles (constant/linear/step/spike) | §4, §12.2 |
| `transport.py` | `Transport` protocol + `HttpxTransport` (real) + `FakeTransport` (scriptable latency/faults, records calls) so the runner is testable | §5.6 |
| `scenarios.py` | Named scenarios binding reader model → request plan: `steady_reader`, `skim_storm`, `seek_thrash`, `cold_open`, `idle_dipper` | §4.10 |
| `runner.py` | The async load engine (open + closed); clock/sleep-injected `VirtualClock` for deterministic tests | §4.9, §12.2 |
| `profiles.py` | Named run presets (scenario + workload template + SLO set) the CLI resolves | §4, §12 |
| `chaos.py` | Deterministic latency/fault/partition injection at the provider/redis/db/blob seams; `transient_then_recover` for retry/DLQ tests | §12.1, §4.11 |
| `capacity.py` | Little's-law render demand · Erlang-C / M/M/c worker sizing · watermark feasibility · §11 budget runway | §4.1, §4.5, §11 |
| `canary.py` | Synthetic-monitoring critical-journey probes (login→library→open→read→seek) + per-step SLA assertions | §13, §5.6 |
| `slo.py` | SLO sets, error budgets, multi-window burn-rate alerting; `slos_from_settings` wires the additive config | §12.5 |
| `runbook.py` | Runbooks-as-code: the §4.11 incident table as executable, dry-run-first playbooks + a `standard_runbooks` registry | §12.1, §4.11 |

## CLI map — `loadtest/`

| Module | Responsibility |
|---|---|
| `_bootstrap.py` | Puts `backend/` on `sys.path` so the CLI can import `app.reliability` from the repo root |
| `__main__.py` | `python -m loadtest`: `--target --profile --users --duration --rps --token --out --dry-run --list-profiles` |
| `canary_cli.py` | `python -m loadtest.canary_cli`: the synthetic critical-journey probe |
| `capacity_cli.py` | `python -m loadtest.capacity_cli`: offline capacity planning (no target, no traffic) |
| `README.md` | CLI usage + toolkit overview |

## Additive shared-file changes

`backend/app/core/config.py` — appended `Settings` fields (defaults; nothing edited):

```
# --- Reliability / load-test / SLO (app.reliability + loadtest) ---
load_default_users: int = 16
load_default_duration_s: float = 60.0
load_default_target_rps: float = 0.0          # 0 => closed model (think-time paced)
load_ramp_seconds: float = 5.0
slo_intent_p99_ms: float = 250.0
slo_seek_coherent_p99_ms: float = 150.0
slo_availability_target: float = 0.995
chaos_default_seed: int = 1337
```

Consumed by `slo.slos_from_settings` (gate tuning without code changes) and
available to the CLI/models as defaults; the CLI overrides them per-run.

## Roadmap / milestones

- [x] **M0 — Recon.** §4/§12, the SSE/WS + intent endpoints, scheduler/queue/budget seams.
- [x] **M1 — Latency digest + metrics report** (`latency.py`, `metrics_report.py`).
- [x] **M2 — Reader model** (`reader_model.py`) — deterministic state machine.
- [x] **M3 — Workload + ramp** (`workload.py`) — open/closed, arrival processes, ramps.
- [x] **M4 — Transport** (`transport.py`) — protocol + `FakeTransport` + thin `HttpxTransport`.
- [x] **M5 — Scenarios** (`scenarios.py`) — the named reader scenarios.
- [x] **M6 — Load runner** (`runner.py`, `profiles.py`) + the `loadtest` CLI.
- [x] **M7 — Chaos library** (`chaos.py`) + a real retry→DLQ resilience test.
- [x] **M8 — Capacity model** (`capacity.py`) + the offline planner CLI.
- [x] **M9 — Canaries + SLO** (`canary.py`, `slo.py`) + the canary CLI.
- [x] **M10 — Runbooks-as-code** (`runbook.py`) + the §4.11 incident registry.
- [x] **M11 — Additive config block** + `slos_from_settings` + README + DESIGN.
- [x] **M12 — Composition tests** (`test_integration.py`): load×chaos, capacity↔reader,
      load→SLO→runbook alignment.
- [ ] **M13+ — Future depth.** A WS-aware transport (drive the §5.6 WebSocket
      `intent_update`/`seek`/`comment` round-trips); an SSE-consume-and-assert
      canary (subscribe to `clip_ready`/`buffer_state` and assert the buffer stays
      above `L`); chaos scenarios composed *inside* the CLI runner (a `--chaos`
      flag); capacity sensitivity sweeps + a CSV export; more runbooks (lease-reaper
      storm, mirror-write failure).

## Test inventory (all under `backend/tests/reliability/`)

`test_latency`, `test_metrics_report`, `test_reader_model`, `test_workload`,
`test_transport`, `test_scenarios`, `test_runner`, `test_profiles`, `test_chaos`,
`test_capacity`, `test_canary`, `test_slo`, `test_runbook`, `test_cli`,
`test_integration` — **175 tests**, deterministic, infra-free.

## Verification

`cd backend && make lint` (ruff + mypy, 400 files) and `make test` (pytest) stay
green. The reliability package and `loadtest/` are pure/deterministic; load is
only ever run by a human via the CLI against a real `--target`. Tests assert the
**models**, not a live server.
