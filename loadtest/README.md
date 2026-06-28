# Kinora Reliability Toolkit (`loadtest/` + `backend/app/reliability/`)

Load generation, chaos injection, synthetic monitoring, capacity planning, and
runbooks-as-code for the **generation-on-scroll** backend (kinora.md §4, §12).

* **Reusable models/probes** live in the backend package `backend/app/reliability/`
  (pure, deterministic, unit-tested — no infra, zero model spend).
* **The CLI** lives here in `loadtest/` and drives those models against an
  **explicitly-provided** target URL.

> **Hard rule:** real load is only ever issued by a human via the CLI with
> `--target`. The unit tests use a fake transport + a virtual clock, so the test
> process never opens a socket and never spends a credit. `KINORA_LIVE_VIDEO`
> stays OFF.

## CLI quick start

Run from the **repo root** (the CLI prepends `backend/` to `sys.path` itself):

```bash
# List the available load profiles
backend/.venv/bin/python -m loadtest --list-profiles

# Dry-run: print the resolved plan (scenario + workload + SLOs), no traffic
backend/.venv/bin/python -m loadtest --profile open_spike --rps 20 --duration 30 --dry-run

# A real closed-model soak against a running backend (gates on SLOs; exit 1 on miss)
backend/.venv/bin/python -m loadtest \
    --target http://localhost:8000 \
    --profile steady_soak --users 16 --duration 60 \
    --token "$KINORA_TOKEN" --out report.json

# Synthetic-monitoring canary (login -> library -> open -> read -> seek)
backend/.venv/bin/python -m loadtest.canary_cli --target http://localhost:8000

# Offline capacity planning (no target, no traffic)
backend/.venv/bin/python -m loadtest.capacity_cli --readers 50 --render-latency-s 60
```

### Load profiles

| Profile | Model | What it stresses |
|---|---|---|
| `steady_soak` | closed | The §4.5 happy-path sawtooth (engaged readers) |
| `skim_storm` | closed | §4.6 trajectory-unstable skim; keyframe ladder under load |
| `seek_thrash` | closed | §4.8 cancel / instant-bridge / re-seed storm |
| `open_spike` | open | A warm-up → 3× spike; exercises §12.2 backpressure |
| `cold_open` | closed | The §4.10 synchronized t=0 committed burst to `H` |

## What's in the toolkit

| Area | Module | Notes |
|---|---|---|
| Load generation | `reliability/reader_model`, `workload`, `scenarios`, `runner`, `profiles` | §4.3 reader state machine → open/closed workloads → scenarios → async engine |
| Reporting | `reliability/latency`, `metrics_report` | mergeable HdrHistogram-style percentiles; throughput / error / status report |
| Chaos | `reliability/chaos` | deterministic latency/fault/partition injection at the provider/redis/db seams |
| Synthetic monitoring | `reliability/canary` | scripted critical-journey probes + per-step SLA assertions |
| Capacity planning | `reliability/capacity` | Little's law / M/M/c queueing / watermark feasibility / budget runway |
| SLOs | `reliability/slo` | SLO sets, error budgets, multi-window burn-rate alerting |
| Runbooks-as-code | `reliability/runbook` | the §4.11 incident table as executable, dry-run-first playbooks |

## Tests

The whole toolkit is unit-tested under `backend/tests/reliability/`:

```bash
cd backend && .venv/bin/pytest tests/reliability -q
```

`make lint` (ruff + mypy) and `make test` stay green. See `DESIGN.md` for the
roadmap and the additive `core/config.py` changes.
