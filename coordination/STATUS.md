# STATUS

> Append-only, agent-scoped sections. Agent 12 reconciles at integration.

## Agent 10 — Reading-room shell (book-open film experience)

Branch: `agent/10-reading-room` · Worktree: `../kinora-a10`

### Progress
- [x] Worktree + deps installed; baseline typecheck + build green
- [x] WS1 — open-state machine (`reading/machine.ts`) — pure, 16 tests green (`node --test`)
- [ ] WS1 — `FilmLoader` / `useFilmSession` load+session orchestration
- [ ] WS2 — `OpenSequence` opening animation (wraps Agent 4 `<BookOpenTransition>`)
- [ ] WS3 — `WarmUp` generation-progress affordance (crew activity / buffered-ahead / skeleton)
- [ ] WS4 — `ReadingRoomShell` compose (top bar, slots, progress rail, focus trap, teardown)
- [ ] `fallback.ts` (no-live-video path) + `crossfade.ts` (never-black film) — pure + tested
- [ ] `producers.tsx` DI (built-ins now; Agent 12 swaps to real Agent 2/4/6 imports)
- [ ] Migrate `components/ReadingRoom.tsx` → `reading/ReadingRoom.tsx` + re-export shim
- [ ] Artifacts: ready-book / mid-ingest / no-backend / close → `coordination/artifacts/agent-12/`
- [ ] Teardown leak check (open/close 10×) ; typecheck + build green ; code review

### Notes / decisions
- Desktop app has **no test runner**; pure logic is TDD'd with Node 26 native TS + `node --test`. Requested vitest from Agent 12 (see `requests/agent-10.md`) for CI parity.
- Built-in stand-ins for Agent 2 (`ScrollFilmEngine`), Agent 4 (`BookOpenTransition`), Agent 6 (`ReadingControls`) live under `reading/builtin/`; the room is **fully functional on its own today**. Agent 12 swaps 3 imports in `reading/producers.tsx` at integration.
- SSE shapes confirmed from backend: `agent_activity {agent,message}`, `buffer_state {committed_seconds_ahead,bursting,inflight_committed,inflight_speculative,zone,...}`, `scene_stitched {scene_id,oss_url,sync_map}`.
