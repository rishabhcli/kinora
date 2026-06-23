# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Kinora turns a book/PDF into a **page-synced film that generates itself a few seconds ahead of the reader**. A crew of six AI agents share one versioned "canon" (story memory) so a long adaptation stays visually consistent. The system never pre-renders the whole film — it renders the *next few seconds* against the reader's attention.

## Frontend rebuild: Electron + React Native (in progress, branch `migrate/electron-rn`)

The product is being rebuilt from a website into **native apps** — Electron (desktop) + Expo/React Native (mobile) — in a pnpm + Turborepo monorepo at the repo root. The Python backend is untouched and is now a pure HTTPS+JWT API the apps call.

- `packages/core/` — framework-agnostic TS shared by both apps: the typed API client (generated from the backend OpenAPI via `pnpm --filter @kinora/core gen:api`), §5.6 event schemas (Zod), the **SyncEngine** (the scroll↔video↔word playhead), the realtime `SessionSocket`, a Zustand-vanilla auth store, and query keys. Tests: `pnpm --filter @kinora/core test` (vitest).
- `apps/desktop/` — Electron (electron-vite + React 18 + Tailwind): auth, library + PDF upload, the two-pane reading room (`PdfReader` + `VideoStage` on the shared SyncEngine), director bar + live crew activity, `safeStorage` token, electron-builder packaging. `pnpm --filter @kinora/desktop build` / `dist`.
- `apps/mobile/` — Expo SDK 56 / React 19 / RN 0.85 (conditional screens, no router yet): login, library, and a reading room (`expo-video` + reflow read-along). `expo-secure-store` token + `eas.json`. **SDK 56 is past the knowledge cutoff — read the v56 docs (`apps/mobile/AGENTS.md`) before changing Expo APIs.**
- Verify everything (the CI `apps` job): `pnpm install && pnpm run typecheck && pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/mobile typecheck && pnpm --filter @kinora/core test && pnpm --filter @kinora/desktop build`.
- The legacy `frontend/` (Vite web app) is **retained until the new apps reach verified runtime parity**, then deleted — don't delete it yet.
- pnpm uses `node-linker=hoisted` (RN/Metro needs it); build-script approvals live in `pnpm-workspace.yaml` under `allowBuilds`.

## The two documents that explain everything

- **`kinora.md`** is the authoritative technical design (architecture, agents, pipeline, memory, budget). The code references its sections everywhere as `§4.5`, `§9.7`, `§8.3`, etc. **When a docstring cites a `§`, read that section of `kinora.md` before changing the code** — the section numbers are the spec the code implements.
- **`README.md`** is the runnable overview (quickstart, process model, the "live video off" loop).

## Commands

All backend tasks go through the root `Makefile` (it drives `backend/.venv`):

```bash
make install          # create backend/.venv + pip install -e .[dev]
make lint             # ruff check + mypy   (run before committing)
make fmt              # black + ruff --fix
make test             # pytest (infra-bound tests skip if no DB/Redis/S3)
make migrate          # alembic upgrade head
make revision REV_MSG="..."   # autogenerate a migration after editing models

# Run a single backend test (from backend/):
.venv/bin/pytest tests/test_scheduler_service.py -q
.venv/bin/pytest tests/test_scheduler_service.py::test_name -q

# Frontend (from frontend/, or `make fe-*` from root):
npm run dev | build | lint | test        # vite / tsc+vite / eslint / vitest
npm run e2e                               # playwright (needs a running backend + seeded book)
```

`make lint` runs `mypy app tests`; CI runs `mypy app` only. Match CI by also keeping `tests` clean.

## Running the stack

```bash
cp .env.example backend/.env     # then set DASHSCOPE_API_KEY=sk-...
make stack-up                    # docker compose up -d --build (whole stack; migrate runs automatically)
make seed-demo                   # load the bundled demo book through the REAL register→upload→ingest flow
# frontend http://localhost:5173 · API http://localhost:8000/docs · Prometheus :9090
```

For venv dev without full Docker: `make stack-up` only the data plane (`docker compose up -d postgres redis minio minio-bootstrap`), `make migrate`, then run `uvicorn app.main:app --reload`, `make worker`, and `make mcp` in separate shells.

## The real process model

Every backend role is the **same image with a different command** (see `infra/docker-compose.yml`). There is **no** standalone scheduler or ingest process:

| Service | Command | Role |
|---|---|---|
| `api` | `uvicorn app.main:app` | REST + SSE/WS; **runs the Scheduler in-process** + the idle-sweeper; **spawns Phase-A ingest** as a background task on PDF upload |
| `render-worker` | `python -m app.queue.worker` | Drains the Redis priority queue; runs the per-shot render pipeline / ffmpeg degradation ladder |
| `mcp` | `python -m app.mcp.run --http` | The canon-memory MCP server |
| `migrate` | `alembic upgrade head` | One-shot schema apply |

A one-shot `python -m app.ingest.worker <book_id>` CLI exists for re-ingest.

## Architecture

**Two deliberately separated planes.** The **control plane** (Scheduler) decides *when and what* to render against the reader's attention; the **creative/data plane** (agent crew + memory + render) decides *how* a scene looks and produces pixels.

### Composition root — start here
`backend/app/composition.py` builds the wired `Container`. This is the single place every dependency-injection **seam** is satisfied with real implementations (`RedisRenderEnqueuer`, the `Adapter` as `ShotPlanner`, the embedder, etc.). Construction is **lazy** — building a `Container` opens no sockets, so `create_app()` and `/health` work with `DASHSCOPE_API_KEY=test` and no network. Collaborators throughout the backend are narrow `Protocol`s exposed as overridable attributes so tests inject light doubles and production uses the real defaults. `app/main.py` is the FastAPI factory that builds the container in its lifespan and launches the idle-sweeper.

### The agent crew (`app/agents/`)
Six single-purpose agents (Showrunner, Adapter, Continuity Supervisor, Cinematographer, Generator, Critic/QA), each a thin service behind a typed Pydantic contract (`contracts.py`). `BaseAgent` owns the shared mechanics: a model id + a *versioned* system prompt (`prompts.py`), JSON-strict calls with exactly one repair round-trip on validation failure, and an optional Qwen function-calling tool loop. **Deterministic policy logic** (render-mode tree §9.3, Critic routing §9.5, conflict arbitration §7.2) lives in the concrete agents as **pure functions**, so it is unit-testable without the runtime.

### Memory = the MCP canon server (`app/memory/` + `app/mcp/`)
The canon (versioned graph of characters/voices/locations/props/style/timeline) + an episodic vector store + a hash-keyed shot cache are the **only shared truth** — no agent holds private mutable state. `app/mcp/tools.py::MemoryTools.dispatch` is the **single execution path** for every tool; both the MCP server (`server.py`) and the in-process Qwen skill dispatcher (`skills.py`) route through it, so there is one thing to test. Postgres + pgvector backs canon/episodic; models in `app/db/models/`, repositories in `app/db/repositories/`.

### Scheduler (`app/scheduler/`)
`service.py::SchedulerService.on_event` is the control tick, run on every debounced intent update or job completion. It idle-pauses quiet readers (§4.7), then **fills the committed video buffer under dual-watermark hysteresis** (low=25s, high=75s of reading-time ahead) — generation is bursty and event-driven, idle between bursts. Three zones: **committed** (full QA-passed video, *spends video-seconds*), **speculative** (one keyframe still per beat, ~zero cost), **cold** (plan + canon only). Promotion is velocity-adaptive and reserves video-seconds from the budget.

### Render pipeline (`app/render/`)
`pipeline.py` runs the per-shot §9.7 state machine: `Promoted → CacheCheck → (hit→Accepted | miss→Rendering→QA→Accepted | Repair→retry≤2 | Conflict | Degraded)`. The **degradation ladder** (`degrade.py`) is a real product feature, not a placeholder: it produces a genuine playable Ken-Burns mp4 over the locked keyframe, muxed with narration, when the live gate is off / budget is low / retries are exhausted.

### Frontend (`frontend/src/`)
React + Vite + TypeScript + Zustand + Tailwind. `sync/SyncEngine.ts` is the client-side single source of truth for the playhead — it bidirectionally binds scroll ↔ video ↔ focus word without a feedback loop, computes reading position `w` and velocity `v`, pushes debounced intent to the Scheduler, and hot-swaps clips as they arrive. It is framework-agnostic (subscribe/getSnapshot for `useSyncExternalStore`) and takes explicit `nowMs` so timing logic is unit-testable. Events arrive over SSE (`/api/sessions/:id/events`) and a Director WebSocket; Vite proxies all of `/api` to the FastAPI gateway on :8000.

## Critical gotchas

- **`KINORA_LIVE_VIDEO` is OFF by default and must stay off unless you intend to spend real Wan credits.** With it off, the *entire* loop still runs end-to-end (ingest → schedule → render Ken-Burns mp4 → events), the budget ledger stays at 0, and tests/CI never burn credits. Flipping it on routes the committed lane through real Wan 2.7 video. Treat turning it on as an explicit, outward-facing action.
- **Budget is a hard ceiling in video-seconds** (`app/memory/budget_service.py`). The Scheduler reserves seconds per promotion; render enforces it. Don't bypass the reservation path.
- **Tests:** the unit suite runs with **no infra** (infra-bound tests skip). Integration tests need `KINORA_TEST_DATABASE_URL` / `KINORA_TEST_REDIS_URL` / `KINORA_TEST_S3_ENDPOINT_URL` (see `tests/conftest.py`, which TRUNCATEs/FLUSHDBs between tests for isolation). Live DashScope smoke tests are gated behind `KINORA_LIVE_TESTS=1` and skip otherwise.
- **Config:** all settings live in `app/core/config.py` (pydantic-settings, loaded from env / `backend/.env`). The only required value is `DASHSCOPE_API_KEY`; everything else has a localhost default. `alembic.ini` leaves `sqlalchemy.url` empty on purpose — it's read from `Settings.database_url` in `migrations/env.py`.
- **MCP auth:** the MCP server requires a bearer token (`MCP_AUTH_TOKEN`) outside `APP_ENV=local`; compose injects a dev token, Terraform injects the real one.
- **`deploy/alibaba_render_worker.py`** is the §12.6 proof-of-deployment artifact — it *reuses* the app's real `ObjectStore` and `VideoProvider` rather than reimplementing the pipeline, so the OSS + DashScope proof stays honest. Don't fork pipeline logic into it.
