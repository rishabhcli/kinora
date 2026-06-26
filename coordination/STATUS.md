# STATUS

> Append-only, agent-scoped sections. Agent 12 reconciles at integration.

## Agent 10 — Reading-room shell (book-open film experience)

Branch: `agent/10-reading-room` · Worktree: `../kinora-a10`

### Progress — COMPLETE
- [x] Worktree + deps installed; baseline typecheck + build green
- [x] WS1 — open-state machine (`reading/machine.ts`) + `useFilmSession` load+session orchestration
- [x] WS2 — opening animation (`builtin/BuiltinOpenTransition`, wraps the Agent 4 `<BookOpenTransition>` slot)
- [x] WS3 — `WarmUp` generation-progress affordance (crew feed / buffered-ahead / monotonic checklist / skeleton)
- [x] WS4 — `ReadingRoomShell` compose (top bar, slots, progress rail, focus trap, Escape, scroll-lock, teardown)
- [x] `fallback.ts` + `crossfade.ts` + `warmupModel.ts` — pure + tested (44/44 `node --test`)
- [x] `producers.tsx` DI (built-ins now; Agent 12 swaps to real Agent 2/4/6 imports)
- [x] Migrated `components/ReadingRoom.tsx` → `reading/ReadingRoom.tsx` + re-export shim
- [x] Artifacts: ready / mid-ingest / no-backend / close + VERIFICATION.md → `coordination/artifacts/agent-12/`
- [x] Teardown leak check (open/close 10× → 0 leaks) ; typecheck + build green
- [x] Slot contract + open-state machine published in `coordination/CONTRACTS.md`

### Verification (with KINORA_LIVE_VIDEO OFF)
Headless Chrome driver (`artifacts/agent-12/verify-driver.mjs`) — all PASS: ready/mid-ingest/no-backend
all play the H.264 film; warm-up resolves into playback; close reverses cleanly; open/close ×10 leaks
nothing (10 SSE opened == 10 closed, 0 net listeners, 0 nodes); no reading-room console errors.

### Notes / decisions
- Desktop app has **no test runner**; pure logic is TDD'd with Node 26 native TS + `node --test`. Requested vitest from Agent 12 (see `requests/agent-10.md`) for CI parity.
- Built-in stand-ins for Agent 2 (`ScrollFilmEngine`), Agent 4 (`BookOpenTransition`), Agent 6 (`ReadingControls`) live under `reading/builtin/`; the room is **fully functional on its own today**. Agent 12 swaps 3 imports in `reading/producers.tsx` at integration.
- SSE shapes confirmed from backend: `agent_activity {agent,message}`, `buffer_state {committed_seconds_ahead,bursting,inflight_committed,inflight_speculative,zone,...}`, `scene_stitched {scene_id,oss_url,sync_map}`.
