# Kinora overnight — agent STATUS

> One section per agent. Keep it short: what's done, what's in flight, what's blocked/needs another agent.
> Created by Agent 07 (Integration Captain / Agent 12 had not yet scaffolded `coordination/`).

---

## Agent 07 — System & Token Optimization
**Branch:** `agent/07-optim` · **Worktree:** `../kinora-a07` · **Base:** `overnight/integration`

### State
- 🟡 In progress.

### Done
- Worktree + `coordination/` scaffolding created (`PERF.md`, `CONTRACTS.md`, `STATUS.md`, `requests/agent-07.md`).
- Baseline inventory of EXISTING infra (so I build on it, not duplicate):
  - `observability/metrics.py` — full Prometheus surface already exists (provider calls/tokens/latency/errors, render, cache hit/miss, queue, scheduler). WS1 raw instrumentation is largely present.
  - `memory/cache_service.py`, `memory/episodic_service.py`, `memory/budget_service.py` — existing cache/budget machinery.
  - `api/routes/metrics.py` is the **eval** route (`/eval/...`), NOT a cost endpoint → my new route must use a different name.

### In flight
- WS1: `optim/cost_meter.py` ($-cost pricing table + per-book/per-session rollups — the gap above the aggregate Prometheus counters).
- WS2: `optim/{cache,routing,prompt_compress,batch}.py` (all default-off, behavior-preserving).
- WS3: hot-path DB indexes migration + throughput patch proposals.
- WS4: `apps/desktop` Vite chunking + `src/lib/perf.ts` + dead-dep removal proposals.

### Needs other agents / Agent 12
- See `coordination/requests/agent-07.md` for file-scoped patch proposals against hot files I don't own
  (`providers/*`, `agents/*`, `scheduler/*`, `queue/*`, repos, `render/*`, client components) + shared seams
  (`composition.py`, `config.py`, app router registration, migration ordering, `package.json`).

---

<!-- Other agents: append your section below. -->
