# Kinora Developer SDKs + Docs Portal — DESIGN.md

> Living roadmap for the `clients/` (typed SDKs + contract spec + drift test) and
> `docs/portal/` (versioned documentation site) developer-experience layer.
>
> **Scope guardrails.** This work lives **only** inside `clients/` and
> `docs/portal/`. It never edits application code (`backend/`, `apps/`). It
> *reads* `backend/app/api/` and `apps/desktop/src/lib/api.ts` to derive the API
> surface, and re-expresses that surface as a single source-of-truth contract.

## 0. The problem

Kinora's REST API is real (FastAPI under `/api`, JWT bearer auth, SSE + WebSocket
event streaming) but the only client is the hand-written renderer client
`apps/desktop/src/lib/api.ts`, which covers a *subset* of the surface and is
coupled to a browser (`localStorage`, `EventSource`, `XMLHttpRequest`). Anyone
building *on top* of Kinora — a CLI, a server-side integration, an alternate
reader UI, a research harness — has no typed, documented, retrying client.

This layer provides:

1. **A single source-of-truth API description** (`clients/spec/kinora-api.ts` +
   generated `clients/spec/openapi.json`) that both SDKs and the docs reference.
2. **A typed TypeScript SDK** (`@kinora/sdk`) — isomorphic (Node 20+ / browser),
   fetch-based, retries + pagination helpers, typed error hierarchy, typed event
   streaming over SSE.
3. **A typed Python SDK** (`kinora`) — sync + async clients on `httpx`, retries,
   typed errors, typed event streaming (SSE), full type hints, `mypy --strict`.
4. **A versioned docs portal** (`docs/portal/`) — dependency-light static site
   generator (plain Markdown + a small Node builder, no heavy toolchain),
   building getting-started, guides, API reference, the six-agent architecture,
   and runnable recipes.
5. **A contract-drift test** (`clients/contract-drift/`) that re-derives the API
   surface from the backend routes and flags when the SDK / spec fall behind.

## 1. The API surface (derived from `backend/app/api/routes/*` + schemas)

All routes are mounted under `/api`. Auth is a JWT bearer token from
`POST /api/auth/login`. Errors are the envelope `{"error": {type, message, detail?}}`.

### Auth (`auth.py`)
- `POST /api/auth/register` → 201 `UserResponse` (email + password >= 8)
- `POST /api/auth/login` → `TokenResponse {access_token, token_type, expires_in}`
- `GET  /api/auth/me` → `UserResponse`  *(bearer)*

### Books / library (`books.py`, `library.py`)
- `POST /api/books` (multipart: file + title?/author?/art_direction?) → 201 `BookResponse`
- `GET  /api/books` → `BookResponse[]`
- `GET  /api/books/{id}` → `BookResponse`
- `GET  /api/books/{id}/pages/{n}` → `PageResponse`
- `GET  /api/books/{id}/canon` → `CanonResponse`
- `GET  /api/books/{id}/shots` → `ShotResponse[]`
- `GET  /api/books/{id}/cover` → 302 redirect to presigned cover

### Films (`films.py`)
- `GET /api/books/{id}/events` → `EventsResponse` (events + restore state)
- `GET /api/books/{id}/scenes/{scene_id}/film` → `SceneFilm`

### Sessions / generation-on-scroll (`sessions.py`)
- `POST /api/sessions` → 201 `SessionResponse`
- `GET  /api/sessions/{id}` → `SessionResponse`
- `POST /api/sessions/{id}/intent` → `IntentResponse`
- `POST /api/sessions/{id}/seek` → `SeekResponse`

### Director tools (`director.py`)
- `POST /api/sessions/{id}/comment` → `CommentResponse`
- `POST /api/books/{id}/canon_edit` → `CanonEditResponse`
- `POST /api/sessions/{id}/conflict_choice` → `ConflictChoiceResponse`
- `GET  /api/sessions/{id}/conflicts` → `ConflictRecordResponse[]`
- `POST /api/sessions/{id}/demo/conflict` → `ConflictRecordResponse` *(local only)*

### Preferences — "Your directing style" (`prefs.py`)
- `GET    /api/me/prefs` → `DirectingStyleResponse`
- `GET    /api/books/{id}/prefs` → `DirectingStyleResponse`
- `DELETE /api/me/prefs` → `ResetPrefsResponse`
- `DELETE /api/books/{id}/prefs` → `ResetPrefsResponse`

### Eval / optim (`metrics.py`, `optim.py`)
- `GET /api/eval/buffer-trace/{session_id}?velocity&duration_s` → `BufferTracePoint[]`
- `GET /api/eval/report/{book_id}` → report object
- `GET /api/optim/cost` → cost rollup
- `GET /api/optim/perf` → perf summary

### Event channel (`events.py`)
- `GET /api/sessions/{id}/events?token=` → SSE stream
- `GET /api/books/events?token=` → SSE library-progress stream
- `WS  /api/ws/sessions/{id}?token=` → bidirectional (intent_update / seek / comment)

SSE event names: `clip_ready`, `buffer_state`, `keyframe_ready`, `scene_stitched`,
`event_stitched`, `agent_activity`, `budget_low`, `regen_done`, `conflict_choice`,
`ingest_progress`.

### Error types (HTTP status → SDK error class)
- 401 → `AuthError`, 402 → `BudgetExceededError`, 403 → `ForbiddenError`,
  404 → `NotFoundError`, 409 → `ConflictError` / `LiveVideoDisabledError`,
  422 → `ValidationError`, 429 → `RateLimitError`, 5xx → `ServerError`,
  408 / network → `TimeoutError` / `NetworkError`.

## 2. Milestones

- [x] **M0 — Foundation.** Scaffold dirs; capture contract; write this roadmap.
- [x] **M1 — Source-of-truth spec.** `clients/spec/kinora-api.ts` (endpoint table +
      event catalog) -> emits `clients/spec/openapi.json`. Generator script.
- [x] **M2 — TypeScript SDK core.** Transport (fetch + retry + timeout), typed
      errors, auth/token store, `KinoraClient` with resource namespaces.
- [x] **M3 — TS SDK events.** Typed SSE parser (isomorphic), event types,
      `streamSessionEvents` async iterator + callback API.
- [x] **M4 — TS SDK tests.** vitest, all HTTP mocked, transport + retries +
      pagination + error mapping + event parsing. `tsc --noEmit` clean.
- [x] **M5 — Python SDK core.** httpx sync + async, typed errors, models
      (dataclasses), retries, `KinoraClient` / `AsyncKinoraClient`.
- [x] **M6 — Python SDK events.** SSE line parser, typed events, sync + async
      iterators. ruff + mypy clean.
- [x] **M7 — Python SDK tests.** pytest + mock transport, zero live calls.
- [x] **M8 — Docs portal generator.** Dependency-light Markdown->HTML static
      builder (Node, no framework). Theme, nav, versioning.
- [x] **M9 — Docs content.** getting-started, auth, guides (generation-on-scroll,
      director, events), API reference (generated from spec), six-agent
      architecture, recipes.
- [x] **M10 — Contract-drift test.** Parse backend routes, compare to spec, fail
      on drift.
- [x] **M11 — Runnable examples.** TS + Python end-to-end example scripts
      (mocked-safe, KINORA_LIVE_VIDEO never required).

## 3. Verification matrix (all green — run `bash clients/verify.sh`)

| Component | Command | Result |
|---|---|---|
| Spec artifacts in sync | `node clients/spec/{generate,sync-ts,sync-py}.mjs --check` | up to date |
| TS SDK typecheck | `cd clients/typescript && npx tsc --noEmit` | clean |
| TS SDK unit tests | `cd clients/typescript && npx vitest run` | **75 passed** |
| TS examples typecheck | `npx tsc -p clients/typescript/tsconfig.examples.json` | clean |
| Python SDK lint | `ruff check clients/python/src` | clean |
| Python SDK types | `mypy clients/python/src/kinora` | clean (`--strict`) |
| Python SDK tests | `pytest clients/python` | **58 passed** |
| Contract drift | `python clients/contract-drift/check_drift.py` | in sync (32 live / 31 doc + 1 WS) |
| Contract-drift tests | `pytest clients/contract-drift` | **8 passed** |
| Docs build | `node docs/portal/build/build.mjs --check` | 14 pages render |
| Docs md renderer | `node --test "docs/portal/build/*.test.mjs"` | **12 passed** |

All SDK tests mock HTTP — **zero live calls**, `KINORA_LIVE_VIDEO` stays OFF.
`clients/verify.sh` runs the whole matrix in one shot.

## 4. Design decisions

- **Single source of truth.** `clients/spec/kinora-api.ts` is the canonical
  endpoint+event description (TS, so it doubles as a typed import for tooling). A
  generator emits `openapi.json` from it. The drift test re-derives the surface
  from the live backend routes and diffs against the spec.
- **Isomorphic TS SDK.** No browser-only globals at import time; `fetch`,
  `AbortController`, `TextDecoder` are standard in Node 20+ and browsers. SSE is
  parsed from a `ReadableStream` (works with `fetch` in both) rather than
  `EventSource`, so it runs server-side and can carry the bearer in a header.
- **Python SDK = httpx.** One dependency. Sync (`httpx.Client`) + async
  (`httpx.AsyncClient`) share a transport core. Models are frozen dataclasses
  built from JSON, tolerant of unknown fields (forward-compatible).
- **Retries.** Idempotent GETs + safe writes retry on 429/502/503/504 + network
  with exponential backoff + jitter, honoring `Retry-After`. Capped attempts.
- **Docs builder.** Pure Node (`.mjs`), zero npm deps — a single-file Markdown
  parser + template. Avoids build-approval churn (`pnpm` native build allowlist),
  per the repo's tooling constraints.

## 5. Remaining / future roadmap

- OpenAPI -> richer JSON Schema component extraction (currently endpoint+ref level).
- Codegen the TS/Python model types directly from `openapi.json` (today the
  models are hand-mirrored from the schemas, kept honest by the drift test).
- WebSocket client in both SDKs (today SSE is first-class; WS documented).
- A `kinora` CLI wrapping the Python SDK.
- Publish pipelines (npm + PyPI) — packaging metadata is in place.
