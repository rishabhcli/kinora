# Kinora — Performance & Cost report (Agent 07)

> **No optimization without measurement.** Every number below is reproducible from a command whose
> output I captured — none are invented. Scripts live in `coordination/benchmarks/`. Where a live
> end-to-end run is infeasible here (no `DASHSCOPE_API_KEY`; `KINORA_LIVE_VIDEO` off by mandate), the
> metric is measured at the **component level** against real code/fixtures and **labelled as such**.

## Environment (measured 2026-06-26)
- Worktree `../kinora-a07` @ `agent/07-optim`, base `overnight/integration` (== `main` @ `4863a0c`).
- Backend Python 3.13.14 (`backend/.venv`); client Node 26.3.1, pnpm 11.7.0, **Vite 5.4.21**.
- Stack up (docker): kinora **Postgres 16 @ `localhost:55432`** (not 5433 — CLAUDE.md stale), redis,
  minio, api:8000. **ffmpeg 8.1.2**. Bundled Chromium (Chrome-for-Testing 149) in the Playwright cache.

---

## BASELINE (untouched `overnight/integration`)

### Backend gates
| Gate | Result |
|---|---|
| `make test` (`pytest -q`) | **301 passed, 125 skipped** ✅ |
| `make lint` (ruff) | **1 error** — `F821 record_conflict_history` @ `tests/test_api_director.py:367` ❌ pre-existing |

Pre-existing lint break (a test forgot to import a function that exists in `app/memory/conflict_log.py`;
infra-skipped so it never NameErrors at runtime, but ruff fails it statically) — blocked the lint gate for
**every** agent. Fixed by adding the one missing import (`requests/agent-07.md` R0). After: `ruff` clean.

### Client bundle (`vite build`, production)
- typecheck ✅ · build ✅ · 459 modules · **total JS+CSS gzip 132,728 B / raw 420,405 B**.
- CSS `index.css` 44,473 B raw / 9,830 B gzip. Biggest chunks: motion-vendor 141.3 KB, react-vendor
  133.9 KB, HomePage (incl. **eager** ReadingRoom) 55.2 KB.

### Existing infra (built on, never duplicated)
Prometheus `observability/metrics.py` (provider tokens/calls/latency/errors, cache, render, queue);
physical-unit `Usage` with an unused `create_providers(usage_sink=...)` seam (← cost meter); content-hash
`shot_cache` clip cache; `BudgetService` video-seconds. No USD anywhere; no model router; canon/page/
embedding reads uncached. Hot DB paths already well-indexed.

---

## AFTER — measured deltas

### WS1 — Cost metering (the money gap above physical units)
`optim/cost_meter.py` (USD `Price` table, pure `cost_of(Usage)`, `CostMeter` usage-sink with per
book/session/model/operation rollups + `cost_context`) + `GET /api/optim/cost|perf` (verified 200 via
TestClient, mounted in `create_app`). 20 tests. Gives per-book/session $ visibility that did not exist;
**behavior-preserving** (observe-only, flag-gated wiring proposed R1). Pricing table is *illustrative*,
tunable via `optim_pricing_json` — ratios/breakdowns are correct regardless of absolute calibration.

### WS2 — Token & model spend  *(component benchmark on REAL prompts + REAL contract schemas)*
`coordination/benchmarks/ingest_token_bench.py`, estimator = stable ~4-chars/token (ratios meaningful):

| Lever (real artifact) | before | after | Δ | quality |
|---|---|---|---|---|
| JSON schema sent per `chat_json` (`ShotListItem`+`ShotSpec`+`Beat`, `compact_json_schema`) | 1283 tok | 576 tok | **−55.1%** | opt-in: strips field *descriptions* → eval-gate (§13) |
| System prompts, whitespace collapse (5 real prompts) | 1410 tok | 1398 tok | −0.9% | neutral |
| Canon re-sends in a 6-shot scene (`dedupe_canon`) | 609 tok | 102 tok | **−83.3%** | neutral (dups carry nothing) |
| **Representative per-shot Cinematographer call** | 1025 tok | 691 tok | **−32.6%** | mixed |

→ ~**334,000 input tokens saved** over a 1,000-shot book ingest. **Cache** (`optim/cache.py`): a re-query of
identical content is served without re-calling the model — re-ingest/re-open hit-rate **100%** for
deterministic page-analysis / canon-query / agent outputs (proven in `test_optim_cache.py`).
**Routing** (`optim/routing.py`): identity by default; opt-in per-site overrides cut $ on the cheapest-
model-that-holds-quality (default-off, eval-gated). All four modules: 52 tests, ruff+mypy clean.

### WS3 — Throughput & latency  *(real ffmpeg degrade lane + real Postgres EXPLAIN)*
**Render throughput** (`render_throughput_bench.py`, real `degrade.ken_burns_over_image`, ffmpeg 8.1.2):
| Lever | before | after | Δ |
|---|---|---|---|
| Lane parallelism (`optim.batch.gather_bounded`, limit 4, 8×3 s clips) | 1.67 clips/s | 2.94 clips/s | **1.76×** |
| Resolution right-sizing — degrade `DEFAULT_SIZE` is **1920×1080** but delivery is vertical **720×1280** | 2.50 clips/s | 4.54 clips/s | **1.82× faster, −33% file size** |

Both proposed to the render owner (Agent 1) — R7 (parallel lane) + R8 (render at delivery size; aligns
with CLAUDE.md "don't push 1080p"). Combined ≈ 3.2× degrade-lane headroom.

**DB index migration** `d9e2f4a6b8c1` — `books(user_id, created_at)`. Verified on a fresh isolated DB
(`alembic upgrade head` applies the full chain 0426→b7d4→c8f1→d9e2; downgrade/upgrade roundtrip OK).
`EXPLAIN ANALYZE` on 2000 books/user (ANALYZE'd):

| shelf query | plan | exec |
|---|---|---|
| `… ORDER BY created_at DESC LIMIT 20` (paginated) | **Index Scan Backward, no Sort** (composite) | **0.013 ms** |
| `… ORDER BY created_at DESC` (full, today's `list_for_user`) | Bitmap Index Scan + quicksort | 0.317 ms |

→ **~24×** for a *bounded* read; realized once the shelf paginates (R6 to Agent 5). Index shipped as the
enabler. **Verified non-beneficial candidates rejected** (no dead indexes): `sessions.last_activity_ms`
(idle sweep is per-row/Redis, never an SQL filter), `shots.accepted_at`, `shots.reference_set_hash` (no
query filters/orders on them). Hot paths (`source_span_index`, episodic HNSW, entity version) already
optimally indexed.

### WS4 — Client weight & runtime
**Bundle** (real `vite build`, `vite.config.ts`: `target esnext` + `modulePreload.polyfill:false`):
- total JS+CSS gzip **132,728 → 132,367 B** (−361 B), raw **420,405 → 417,009 B** (−3,396 B); `index.css`
  44.47 → 41.65 KB (lightningcss drops legacy fallbacks at the modern target). Build still green. Modest —
  the bundle is vendor-dominated; the large wins are source-level (below).
- **R5 (lazy-split ReadingRoom)** measured experimentally (applied, built, reverted): initial **HomePage
  chunk 55.2 → 39.9 KB raw / 15.9 → 11.2 KB gzip** (**−15.3 KB / −4.7 KB**); ReadingRoom (15.6 KB / 5.8 KB
  gzip) deferred to book-open. Proposed to Agent 2/10.
- **R4** dead weight (confirmed zero import sites): dep `lucide-react`; components `BlobRainAnimation`,
  `RainAnimation`, `BookTicker`, `FloatingDock` (tree-shaken today → removal cleans source/install).

**Reading-room FPS** (`fps_harness.html`, the real CrossfadeFilm primitives — two-layer opacity crossfade
+ `translate3d` scroll over a 600-node word tree; bundled Chromium headless, 720×1280, 180-frame rAF):

| technique | rAF fps | frame interval | main-thread work/frame |
|---|---|---|---|
| **Reading-room (opacity crossfade + transform, GPU-composited)** | **119 fps** | 8.3 ms | **~0 ms** (median 0, p95 0.1, max 0.1) |
| Layout-thrash anti-pattern (per-frame geometry + forced reflow) | 40 fps | 25 ms | 1.1 ms (max 2.8) |

→ The reading room's technique sustains the **full display refresh (60 & 120 Hz) with the entire frame
budget free**; the contrast shows the harness genuinely measures jank. `perf.ts` (`preloadVideo`/
`decodeOnIdle`) keeps the only remaining risk — first-frame video decode — off the critical path.

`perf.ts` opt-in helpers (`lazyImport`/`preloadVideo`/`decodeOnIdle`/`mark`/`measure`) ship typecheck-clean.

---

## Methodology / honesty ledger
- **Bundle, migration, render throughput, FPS**: measured directly (vite build / alembic+EXPLAIN /
  ffmpeg / headless Chromium). Reproducible from `coordination/benchmarks/`.
- **Ingest token/call**: component benchmark on **real** prompts + **real** contract schemas through the
  real `optim.prompt_compress` — **not** a live DashScope ingest (no key). Estimator is the stable
  ~4-chars/token heuristic, so before/after **ratios** are valid (absolute token counts are approximate).
- **FPS**: headless Chrome-for-Testing on an M3 Max → rAF is display-capped (~120 Hz); the meaningful,
  discriminating figure is **per-frame main-thread work** (governs dropped frames), reported alongside.
- **All cross-file / risky changes** are flag-gated proposals in `requests/agent-07.md`, not direct edits.
