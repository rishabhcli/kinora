# Integration status

> One section per agent. Agent 12 reads this to integrate onto `overnight/integration`.

---

## Agent 01 — Event Director, Stitching & Video Generation — ✅ core complete

**Branch:** `agent/01-event-director` (worktree `../kinora-a01`).

### Shipped

| Workstream | Status | Where |
|---|---|---|
| Vertical 720×1280 enforcement (the stitch bug) | ✅ | `degrade.FILM_SIZE`, `stitch.concat_clips` |
| **WS1** Event Director: plan → N parallel shots → ONE film | ✅ | `render/event_director.py` |
| WS1 last-frame continuity chaining (`lastframes/{book}/{shot}.png`) | ✅ | `event_director.plan_event_script` + `_persist` |
| **WS2** Cinematic stitch: xfade + audio crossfade + level-normalise | ✅ | `stitch.concat_clips(crossfade_s=)`, `merge_sync_segments(overlap_s=)` |
| **WS3** Deterministic continuity QA (5 seam checks, repair routing, supplemental) | ✅ | `render/continuity_qa.py` |
| WS3 shot grammar: establishing→insert, screen-direction, 180° rule | ✅ | `render/shot_grammar.py` |
| Per-shot degrade renders vertical (no resolution jump end-to-end) | ✅ | `render/pipeline.py::_degrade` |

### Verification

- `make lint` (ruff + mypy) green; `make test` green (render suite: stitch, event director,
  continuity QA, shot grammar, degrade, pipeline, sync map).
- **DoD golden test:** `test_event_director_stitches_three_ken_burns_into_one_vertical_film`
  — 3 Ken-Burns shots render concurrently → ONE 720×1280 mp4; last segment `video_end_s`
  == film duration ±1 frame; film + last-frame anchors persisted.
- Live artifact in `coordination/artifacts/agent-01/` (regenerable offline, zero spend).
- **Baseline lint fixes (heads-up for Agent 12):** `make lint` was already red on the
  base commit — `test_api_director.py` used `record_conflict_history` without importing
  it (F821) + typed a `pubsub` param as `object`; `test_prefs_learning.py` did
  `"x" in grade_filter(...)` on a `str | None`. Fixed (trivial, test-only) so the suite
  is green; none touch a DO-NOT-TOUCH file.

### Contracts published

`coordination/CONTRACTS.md` § Agent 01 — `EventScript` / `EventShot` / `ContinuityDirective`,
the event `SceneSyncMap`, the `EventDirector` producer API, and the suggested HTTP shape
for Agent 3.

### Hand-offs requested (Agent 12)

See `coordination/requests/agent-01.md`: (1) wire the worker/scheduler to call
`EventDirector` for event-grained films, (2) register Agent 3's event-film route,
(3) no migrations required. All are additive — nothing in Agent 1's lane blocks others.

### Not in scope here (by design)

- HTTP routes/serialization (`routes/films.py`, `lib/api/films.ts`) — **Agent 3**.
- The live Wan renderer behind `EventShotRenderer` — gated off (`KINORA_LIVE_VIDEO`);
  the Protocol seam is ready for it. Off-gate Ken-Burns proves the pipeline at zero spend.
