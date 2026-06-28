# Render engine — hardening, observability, resumability (§9.2–§9.7, §4.4)

**Domain owner:** Render pipeline §9.7 state machine.
**Owned files:** `pipeline.py`, `states.py`, `degrade.py`, and new orchestration
modules added here (`checkpoint.py`, `ladder.py`, `dag.py`, `simulator.py`,
`telemetry.py`, `poison.py`, `retry.py`, `engine.py`). Round-1 siblings
(`conflict.py`, `continuity_qa.py`, `shot_grammar.py`, `cinematic_language.py`,
`stitch.py`, `sync_map.py`, `qa/`, `reward.py`, `continuity_reasoning/`) are
**called, never edited**.

This document is a living roadmap. Each phase lands real, tested code and keeps
`make lint` + `make test` green. `KINORA_LIVE_VIDEO` stays OFF throughout.

---

## What already exists (round-1, build on it, don't fight it)

- `RenderPipeline.render_shot` (`pipeline.py`) — the full §9.7 per-shot loop:
  cache probe → design → live Wan loop (reserve/render/QA/repair/conflict) →
  accept | degrade. Returns a `RenderResult`.
- `ShotStateMachine` (`states.py`) — validated §9.7 edges + a persist hook +
  transition history.
- `degrade.py` — the real ffmpeg ladder primitives (`ken_burns_over_image`,
  `audio_text_card`, `extract_frames`, `probe`/`inspect`/`verify_playable`,
  camera→zoom / pacing→dwell / palette→grade helpers).
- `app/queue/worker.py` (NOT ours) — claims jobs, calls `render_shot`, retries /
  DLQs, stitches scenes. We expose APIs it (and tests) can consume.
- `app/observability/metrics.py` (shared) — Prometheus surface. Additive only.

## Design principles

1. **Wrap, don't rewrite.** The hardening layers (checkpoint, ladder planner,
   DAG, simulator, telemetry) sit *around* the existing `RenderPipeline` and its
   collaborator Protocols. The happy path stays byte-for-byte identical.
2. **Determinism.** Ladder selection, retry policy, DAG ordering, and the
   simulator are pure functions of their inputs — unit-testable without ffmpeg,
   DB, Redis, or DashScope.
3. **Idempotency + resumability.** Every expensive step is keyed by a stable id
   so a restart re-derives the same plan and skips already-done work.
4. **Observability first.** Every state, rung, retry, checkpoint, and poison
   decision emits a typed telemetry event; the simulator replays them offline.

---

## Phases

### Phase 1 — Ladder planner as first-class lanes  (`ladder.py`)
A pure, deterministic `LadderPlan`/`plan_ladder` that enumerates the §4.4/§12.4
rungs as ordered *lanes* (FULL_WAN → KEN_BURNS_KEYFRAME → KEN_BURNS_ILLUSTRATION
→ AUDIO_TEXT_ONLY), each with its required inputs, cost class, and a guard
predicate. Given the available assets + the pressure reason it returns the
highest feasible rung and the full fallback chain. Decouples "what rung is
reachable" from "render it", so both the live pipeline and the simulator share
one selection brain. Unit-tested without ffmpeg.

### Phase 2 — Per-state telemetry bus  (`telemetry.py`)
A typed `RenderEvent` stream (`StateEntered`, `RungSelected`, `RetryScheduled`,
`Checkpointed`, `Poisoned`, `StepSkipped`, `ShotFinished`) with an in-memory
recorder + a metrics sink that fans out to `app/observability/metrics.py`. The
state machine and the hardening layers publish here; the simulator consumes the
same events. Bounded, thread-safe, JSON-serialisable for the §13 demo panel.

### Phase 3 — Idempotent step ledger  (`steps.py`)
A `StepLedger` of named, content-addressed steps (`reserve`, `generate`, `qa`,
`persist_clip`, `cache_put`, …). `run(name, key, fn)` returns the recorded result
when `key` already done, else runs `fn` and records it. Makes a mid-shot crash
safe to retry without double-spending video-seconds or double-writing OSS.

### Phase 4 — Checkpoint / restore of in-flight shots  (`checkpoint.py`)
A `ShotCheckpoint` snapshot (shot_id, state, attempts, spent video-seconds,
last rung, step ledger, reason) + a `CheckpointStore` Protocol with an in-memory
implementation and a JSON codec. Loading a checkpoint for a terminal shot makes
resume a no-op (idempotent). Restart-safe.

### Phase 5 — Deterministic retry / repair policy  (`retry.py`)
Pure `RetryPolicy`/`decide_retry` mirroring the §9.5 routing + retry-cap +
deterministic backoff so the escalation is unit-testable in isolation and reused
by the DAG scheduler + poison handler. Classifies permanent vs transient.

### Phase 6 — Render-graph DAG for parallel shots  (`dag.py`)
A `RenderGraph` of shot nodes with dependency edges (a `video_continuation` shot
depends on its predecessor's *accepted* endpoint; same-scene ordering). A
deterministic topological scheduler yields ready-batches honouring a concurrency
cap, so independent shots render in parallel while continuation chains stay
ordered. Cycle + orphan detection. Pure planning; execution delegates to a runner.

### Phase 7 — Dead-shot / poison handling  (`poison.py`)
A `PoisonTracker` counting a shot's hard failures across attempts/restarts; it
quarantines a shot that repeatedly crashes the renderer (distinct from a clean
degrade), forcing the bottom rung and logging a `poison` defect so one
pathological shot can never wedge a lane or crash-loop the budget.

### Phase 8 — Deterministic pipeline simulator  (`simulator.py`)
A zero-IO `RenderSimulator` driving the §9.7 state machine + ladder planner +
retry policy over a scripted scenario (QA verdict sequence, budget/live gate,
available assets, injected failures) → a `SimReport` (final state, rung,
attempts, video-seconds, full event trace). Proves control flow incl. poison +
resume without ffmpeg/DB/network; powers a "what-if" demo panel.

### Phase 9 — Engine facade  (`engine.py`)
A thin `RenderEngine` composing the resumable runner + telemetry + poison tracker
+ step ledger over a `RenderPipeline`, exposing `render_shot` with the same
signature so `worker.py` can opt in, plus a graph-driven `render_scene`.

---

## Additive shared-file changes (per the parallel-agent rules)

- `app/observability/metrics.py`: ADD render-engine series
  (`checkpoint_total`, `poison_total`, `step_skipped_total`, `dag_batch_size`,
  `resume_total`). Additive only — no existing series touched.
- `app/core/config.py`: ADD render-engine knobs (`render_checkpoint_enabled`,
  `render_poison_threshold`, `render_max_parallel_shots`) with safe defaults; no
  existing field changed.

## Test plan

New suites under `backend/tests/`: `test_render_ladder.py`,
`test_render_telemetry.py`, `test_render_steps.py`, `test_render_checkpoint.py`,
`test_render_retry.py`, `test_render_dag.py`, `test_render_poison.py`,
`test_render_simulator.py`, `test_render_engine.py`. Pure-logic suites need no
ffmpeg; the engine suite reuses `tests.test_render_support` doubles. All keep
`KINORA_LIVE_VIDEO` off.

## Status

- [x] Phase 1 — ladder planner (`ladder.py`, 24 tests)
- [x] Phase 2 — telemetry bus (`telemetry.py`, 7 tests)
- [x] Phase 3 — step ledger (`steps.py`, 8 tests)
- [x] Phase 4 — checkpoint/restore (`checkpoint.py`, 9 tests)
- [x] Phase 5 — retry policy (`retry.py`, 16 tests)
- [x] Phase 6 — render DAG (`dag.py`, 11 tests)
- [x] Phase 7 — poison handling (`poison.py`, 8 tests)
- [x] Phase 8 — simulator (`simulator.py`, per-shot + scene, 19 tests) +
      `states.step()` sync edge
- [x] Phase 9 — engine facade (`engine.py`, 11 tests incl. real-pipeline drop-in
      + `build_render_engine` production factory)

**All phases landed. `make lint` + `make test` green (1154 passed, 160 skipped).**
New modules: `ladder.py`, `telemetry.py`, `steps.py`, `checkpoint.py`, `retry.py`,
`dag.py`, `poison.py`, `simulator.py`, `engine.py` (+ `states.step()` sync edge).
Additive shared-file changes landed: `metrics.py` (5 new series + emit helpers),
`config.py` (3 new knobs). ~112 new tests; full render suite 137 passing.

### How a worker opts in (no worker edit needed)
`RenderWorker(..., run_shot=engine.render_shot)` — the engine speaks the
pipeline's `render_shot` signature. `build_render_engine(session, providers=…,
object_store=…)` wires the production engine over `build_render_pipeline` and
attaches the metrics+log telemetry sinks.

## Remaining roadmap (post Phase 9)

- **Durable stores:** a Redis `CheckpointStore` adapter (`JsonCheckpointStore` is
  ready — drop in the real `RedisClient`) and a Redis `PoisonStore`, so quarantine
  + resume survive a worker restart, not just within-process.
- **Worker flip:** pass `run_shot=engine.render_shot` in `build_worker` behind a
  setting (the factory + drop-in transparency are already proven). Owned by the
  queue domain — additive `run_shot` injection, no worker logic change.
- **DAG-aware scene backfill command** (re-render a whole book deterministically
  via `RenderEngine.render_scene` over the book's scenes).
- **Simulator-backed property tests** (hypothesis) over the §9.7 edge set: every
  random QA/budget/asset/crash scenario must reach a legal terminal state with a
  monotone, gap-free event trace.
- **§13 demo panel:** expose `RecordingSink.as_dicts()` + `SceneReport`/`LadderStats`
  over an SSE endpoint (owned by the API domain) so the "what-if" / ladder-
  distribution view renders live.
