# STATUS

> One section per agent. Agent 12 reads this to integrate.

---

## Agent 03 — Film API, Sync Map & Client Wiring

**Branch:** `agent/03-film-api` (worktree `../kinora-a03`, base `overnight/integration`).

### State: ✅ COMPLETE — all DoD gates green

| WS | Item | State |
|----|------|-------|
| — | Contract published (`coordination/CONTRACTS.md` §Agent-03) | ✅ |
| — | Cross-seam requests (`coordination/requests/agent-03.md`) | ✅ |
| WS1 | `backend/app/films/contract.py` — wire models + builders (7 unit tests) | ✅ |
| WS1 | `backend/app/api/routes/films.py` — `GET /events`, `GET /scenes/{id}/film` | ✅ |
| WS1 | `backend/tests/test_api_films.py` — 7 integration tests (real isolated infra) | ✅ |
| WS2 | `apps/desktop/src/lib/api/films.ts` + `http.ts` shim | ✅ |
| WS2 | `apps/desktop/src/lib/api/films.typecheck.ts` — consume proof (compiles) | ✅ |
| WS3 | SSE `event_stitched` / `scene_stitched` payloads + builders + TS types | ✅ |
| DoD | `make lint` ✅ · `make test` ✅ (308 passed) · desktop typecheck ✅ · build ✅ | ✅ |

Evidence: `coordination/artifacts/agent-03/verification.md`. Examples: `…/example-responses.json`.

### Code review (senior-reviewer subagent) — passed with fixes applied
No Critical issues. Important findings all addressed: (1) corrected the SSE builder signature +
two-step `film_sync_map_from_merged` recipe in CONTRACTS.md §5 / requests (the emit itself is
Agent 1's worker — flagged); (2) ordered shots by `Beat.beat_index` to match the stitcher exactly
(+ adversarial test pinning it); (3) documented restore's nearest-preceding-shot semantics. Minor
items (clamp note, key-namespace note, duration_s vs sync_map.duration_s) documented in code/types.
17 film tests green (10 route + 7 contract).

### Design decisions (rationale in CONTRACTS.md)
- **event ≡ scene (1:1)** today; `EventFilm.scenes[]` is forward-compatible for grouping.
- Own API/wire models in `app/films/contract.py` (render's `SyncSegment` is `extra="forbid"`,
  can't be extended); mirror — not import — render so the module survives Agent 1's churn.
- Sync map built **on read** from accepted shots; works with `KINORA_LIVE_VIDEO` off.
- `films.ts` uses a local `http.ts` shim (base `api.ts` has no `http` export, Agent 12's lane).
- Routes tested via a locally-assembled app (router registration is Agent 12's lane).

### For Agent 2
`films.ts` types mirror the JSON 1:1 (snake_case). `getEvents`/`getSceneFilm` return typed
objects; `scene_stitched`/`event_stitched` SSE frames share the same `FilmSyncMap` type. See
`artifacts/agent-03/README.md` for the seek/karaoke/hot-swap consumption snippets.

### For Agent 12 (two asks in `requests/agent-03.md`)
1. Add `films.router` to `backend/app/api/routes/__init__.py` ROUTERS.
2. Expose `http` from `lib/api.ts` (then delete the shim + flip one import in `films.ts`).

### Heads-up (base fixes applied, outside my lane)
To make the shared `make lint` green I fixed three **pre-existing base** errors: a missing
`record_conflict_history` import in `tests/test_api_director.py` (F821) and two mypy nits
(`test_api_director.py` `_collect` pubsub type, `test_prefs_learning.py` `grade_filter` narrowing).
Minimal + type-only; please keep if you re-touch those files.
