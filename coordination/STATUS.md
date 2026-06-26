# STATUS

> One section per agent. Agent 12 reads this to integrate.

---

## Agent 03 — Film API, Sync Map & Client Wiring

**Branch:** `agent/03-film-api` (worktree `../kinora-a03`, base `overnight/integration`).

### State: IN PROGRESS

| WS | Item | State |
|----|------|-------|
| — | Contract published (`coordination/CONTRACTS.md` §Agent-03) | ✅ |
| — | Cross-seam requests (`coordination/requests/agent-03.md`) | ✅ |
| WS1 | `backend/app/films/contract.py` — wire models + builders | ⏳ |
| WS1 | `backend/app/api/routes/films.py` — `GET /events`, `GET /scenes/{id}/film` | ⏳ |
| WS1 | `backend/tests/test_api_films.py` — route tests (TDD) | ⏳ |
| WS2 | `apps/desktop/src/lib/api/films.ts` + `http.ts` shim | ⏳ |
| WS2 | `apps/desktop/src/lib/api/films.typecheck.ts` — consume proof | ⏳ |
| WS3 | SSE `event_stitched` / `scene_stitched` payloads + builders | ⏳ (in contract.py) |
| DoD | `make lint && make test` + desktop typecheck/build | ⏳ |

### Design decisions (rationale in CONTRACTS.md)
- **event ≡ scene (1:1)** today; `EventFilm.scenes[]` is forward-compatible for grouping.
- Own API/wire models in `app/films/contract.py` (render's `SyncSegment` is `extra="forbid"`,
  can't be extended) — but **reuse** `app.render.stitch.merge_sync_segments` for cumulative offsets.
- Sync map built **on read** from accepted shots; endpoint never blocks on rendering.
- `films.ts` uses a local `http.ts` shim (base client `api.ts` has no `http` export and is
  Agent 12's lane) — see `requests/agent-03.md`.
- Routes tested via a locally-assembled app (router registration is Agent 12's lane).

### For Agent 2
`films.ts` types mirror the JSON 1:1 (snake_case). `getEvents`/`getSceneFilm` return typed
objects; `scene_stitched`/`event_stitched` SSE frames share the same `FilmSyncMap` type.

### For Agent 12
Two cross-seam asks in `requests/agent-03.md`: (1) add `films.router` to ROUTERS;
(2) expose `http` from `lib/api.ts` (then delete the shim, flip one import).
