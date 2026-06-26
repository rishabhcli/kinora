# Kinora — Performance & Cost report (Agent 07)

> **No optimization without measurement.** Every number below is reproducible from a command whose
> output I captured — none are invented. Where a live end-to-end run is infeasible in this environment
> (no `DASHSCOPE_API_KEY`; `KINORA_LIVE_VIDEO` off by mandate), the metric is measured at the
> **component level** against real code/fixtures and **labelled as such** — never presented as a live run.

## Environment (measured 2026-06-26)
- Worktree `../kinora-a07` @ branch `agent/07-optim`, base `overnight/integration` (== `main` @ `4863a0c`).
- Backend: Python 3.13.14, `backend/.venv` (editable install OK).
- Client: Node v26.3.1, pnpm 11.7.0, Vite 5.4.21.
- Infra UP locally: **Postgres `localhost:5433`**, **Redis `localhost:6379`**, **ffmpeg 8.1.2** (`/opt/homebrew/bin/ffmpeg`).

---

## BASELINE (untouched `overnight/integration`)

### Backend gates
| Gate | Command | Result |
|---|---|---|
| Tests | `make test` (`pytest -q`) | **301 passed, 125 skipped**, 6 warnings, 26.8s ✅ |
| Lint (ruff) | `ruff check app tests scripts` | **1 error** — `F821 undefined name record_conflict_history` @ `tests/test_api_director.py:367` ❌ (pre-existing) |
| Lint (mypy) | `mypy app tests` | (ran after ruff; ruff failed the gate first) |

> The lint failure is **pre-existing baseline breakage** (the test forgot to import a function that
> exists in `app/memory/conflict_log.py`; the test is infra-skipped so it never NameErrors at runtime,
> but ruff catches it statically). It blocks the shared lint gate for **every** agent. Fixed by adding
> the one missing import (see `requests/agent-07.md` → flagged for the director-test owner). After fix:
> `ruff check` → **All checks passed!**

### Client bundle (`vite build`, production)
- typecheck `tsc --noEmit` ✅ exit 0 · `vite build` ✅ exit 0 · 459 modules.
- **Total JS+CSS, raw: 420,405 B · gzipped: 132,728 B** (the transfer-weight figure).
- CSS: `index.css` → **44,473 B raw / 9,830 B gzip** (single 1230-line stylesheet, 22 keyframes).

| Chunk | raw B | gzip B |
|---|---|---|
| motion-vendor (framer-motion) | 141,288 | 46,770 |
| react-vendor | 133,922 | 43,120 |
| HomePage (incl. **eager** ReadingRoom) | 55,257 | 15,900 |
| index.css | 44,473 | 9,830 |
| index (entry) | 21,950 | 7,970 |
| 7 lazy pages (Watch/Pricing/Notes/EditProfile/Settings/Library/Favorites) | 23,515 | 8,860 |

Pre-existing build config: plain Vite, `react()` only; `manualChunks` already splits `react-vendor` +
`motion-vendor`; `build.target: es2020`; `cssMinify: lightningcss`. No bundle analyzer installed.
Dead weight (zero import sites, confirmed): dep `lucide-react`; components `BlobRainAnimation`,
`RainAnimation`, `BookTicker`, `FloatingDock` (tree-shaken out today ⇒ removing them cleans source but
does not shrink the bundle; `lucide-react` removal shrinks install only).

### Existing infra inventory (so I build on it, never duplicate)
- **Prometheus** (`observability/metrics.py`): provider calls/tokens/latency/errors, render latency/mode,
  cache hit/miss, video-seconds, queue depth, scheduler watermarks — raw instrumentation already rich.
- **Cost is tracked in physical units only** (`providers/types.py:Usage`: tokens/images/audio_s/video_s).
  **No USD/pricing anywhere.** `UsageSink = Callable[[Usage], None]` is a *designed, unused* seam
  (`create_providers(usage_sink=...)`) → the clean injection point for a cost meter. ← my WS1 gap.
- **Content-hash clip cache exists** (`memory/cache_service.py` + `shot_cache` table). Do NOT duplicate.
  **Nothing caches** `CanonService.query` / page analysis / embeddings reads → my WS2 cache is net-new.
- **No model router** — each agent hard-binds its model at construction (`showrunner→max`,
  `continuity→plus`, `adapter/cinematographer→adapter`, `critic→vl`). ← my WS2 routing gap.
- DB hot paths (`source_span_index`, `shots`) are **already well-indexed**; only narrow, verified
  candidates remain (see WS3) — I will not add dead indexes.

---

## AFTER (optimizations — filled as landed, baseline→after with deltas)

### WS1 — Cost metering ($ on top of physical units)
- _pending: `optim/cost_meter.py` rollups + `/api/optim/cost` endpoint._

### WS2 — Token & model spend
- _pending: cache hit-rate on re-query; prompt-compress token delta on real fixtures; routing $ delta on a representative call mix._

### WS3 — Throughput & latency
- _pending: verified hot-path indexes (migration applies via `make migrate`); degrade-lane throughput (ffmpeg) before/after; in-flight render dedup._

### WS4 — Client weight
- _pending: bundle gzip before→after from real `vite build`; reading-room FPS via instrumented harness._

---

## Methodology notes (honesty ledger)
- **Bundle size**: real `vite build` output, gzipped, before vs after.
- **Ingest token/call**: component benchmark — real agent prompt templates + a seeded book's text, token
  counts (via a deterministic estimator) before/after prompt-compress + cache; **not** a live DashScope
  ingest (no key). Labelled component-level.
- **Render throughput**: real ffmpeg degrade-lane micro-benchmark (Ken-Burns mp4, the off-gate path that
  the README says runs end-to-end) and/or queue-throughput sim; before/after a throughput change.
- **Reading-room FPS**: instrumented via `perf.ts` marks + a measurable animation harness; methodology
  documented with the captured number.
