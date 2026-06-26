# MISSION — AGENT 3: Film API, Sync Map & Client Wiring You own the **HTTP + client boundary** between Agent 1's stitched event films and Agent 2's scroll engine. Define and ship the **event film contract**: one stitched mp4 URL + ordered sync map `{ shot_id, scene_id, word_range, t_start_s, t_end_s }[]`, plus SSE payloads for `event_stitched` / `scene_stitched`. You do **not** own stitch logic (Agent 1) or scroll UI (Agent 2). You wire the data path both sides can rely on. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** FastAPI routing & response models, Pydantic v2, SSE patterns. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## GROUND TRUTH
- Read `CLAUDE.md`, `kinora.md` §9.6. Reuse `object_store` presigned URLs.
- Vertical **720×1280** films. Client uses `toBrowserUrl()` from `lib/api.ts` (import `http` only — do not edit base client; Agent 12 owns it).
- **`KINORA_LIVE_VIDEO` OFF.** Tests: isolated DB, redis db 15, Postgres **5433**. ---

## SYSTEM DESIGN (your lane)
- **Video URLs:** serve presigned object-store URLs (local MinIO; cloud bucket in production). Document URL lifetime + refresh pattern in contract.
- **Project state on fetch:** `GET /api/books/{id}/events` should include enough state for Agent 12 to restore open-book context (current event index, last sync position) — coordinate field names in `CONTRACTS.md`. ---

## YOUR LANE — OWNERSHIP **Backend:**
- NEW `backend/app/api/routes/films.py` — `GET /api/books/{id}/events`, `GET /api/books/{id}/scenes/{scene_id}/film`, SSE-friendly stitched payloads. **Client:**
- NEW `apps/desktop/src/lib/api/films.ts` — typed methods; `import { http } from '../api'`. **DO NOT TOUCH:** `render/` (Agent 1), `ScrollFilmEngine` (Agent 2), `lib/api.ts` base (Agent 12), app router registration (Agent 12). **Shared seams → `coordination/requests/agent-03.md`:** router include, migrations if needed. ---

## CONTRACTS
- **You PUBLISH (authoritative in `coordination/CONTRACTS.md`):** event film JSON schema, sync map types, TypeScript types in `films.ts`, SSE event shapes.
- **You CONSUME:** Agent 1's stitch output format and object-store keys. ---

## THE BUILD

### WS1 — REST endpoints
- List events for a book with stitched URL + sync map per event/scene.
- Single-scene film endpoint for partial loads.
- Presigned URL refresh semantics documented.

### WS2 — Client API
- `films.ts`: `getEvents(bookId)`, `getSceneFilm(bookId, sceneId)`, typed responses matching contract.
- Unit tests or typecheck proofs that Agent 2 can consume without adapters.

### WS3 — Realtime
- SSE payload for `event_stitched` / `scene_stitched` compatible with Agent 12's session stream. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 03 COMPLETE</promise>` 1. `make lint && make test` + desktop typecheck/build green. 2. Contract published; Agent 2 can import `films.ts` against stub or real backend. 3. `coordination/STATUS.md` updated. Artifacts in `coordination/artifacts/agent-03/`.

## GIT WORKTREE | **Worktree** | `../kinora-a03` | | **Branch** | `agent/03-film-api` | 
```bash
git worktree add ../kinora-a03 -b agent/03-film-api overnight/integration cd ../kinora-a03
``` Cross-seam: `coordination/requests/agent-03.md`. End commits with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
