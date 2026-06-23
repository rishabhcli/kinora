# GEMINI.md

This file provides guidance to the Gemini CLI when working with code in this repository.

Kinora turns a book/PDF into a **page-synced film that generates itself a few seconds ahead of the reader** — six AI agents share one versioned "canon" so a long adaptation stays visually consistent. The product is **native apps** (Electron desktop + Expo/React Native mobile) over a cloud **FastAPI** backend. There is no web frontend (the legacy Vite app was retired).

## Authoritative docs
- **`kinora.md`** — the technical design (architecture, agents, pipeline, memory, budget). The backend cites its sections as `§4.5`, `§9.7`, etc. **When a docstring cites a `§`, read that section before changing the code.**
- **`README.md`** — runnable overview + how to bring up the backend and run the apps.

## Repository shape (pnpm + Turborepo monorepo at the root)
- `backend/` — FastAPI app, six-agent crew, MCP canon server, render pipeline, scheduler + Redis queue, Alembic. Python 3.11+. **Not** a pnpm workspace member.
- `packages/core/` — shared TypeScript for both apps: the typed API client (generated from the backend OpenAPI), §5.6 event Zod schemas, the **SyncEngine** (scroll↔video↔word playhead), the realtime `SessionSocket`, a Zustand-vanilla auth store, and query keys.
- `apps/desktop/` — Electron (electron-vite + React 18 + Tailwind): auth, library + PDF upload, two-pane reading room, director bar + live crew activity, `safeStorage` token, electron-builder packaging.
- `apps/mobile/` — Expo SDK 56 / React 19 / RN 0.85: auth, library, reading room (expo-video + reflow read-along), expo-secure-store, `eas.json`. **SDK 56 is past the training cutoff — read the v56 docs (`apps/mobile/AGENTS.md`) before changing Expo APIs.**
- `infra/` — docker-compose (the backend stack) + `terraform/`. `deploy/` — the §12.6 Alibaba proof worker. `assets/books/` — the bundled demo PDF.

## Commands
Backend — root `Makefile` (drives `backend/.venv`):
- `make install` · `make lint` (ruff + mypy) · `make fmt` · `make test` (pytest) · `make migrate`
- Single backend test: `backend/.venv/bin/pytest tests/test_x.py::test_name -q`

Apps — pnpm + Turborepo (from the repo root):
- `make app-install` (= `pnpm install`)
- `make app-typecheck` — typecheck core + desktop + mobile
- `make app-test` — `pnpm --filter @kinora/core test` (vitest)
- `make app-desktop-build` / `app-desktop-dev` — `electron-vite build` / `dev`
- `make app-mobile-start` — `expo start`
- `pnpm --filter @kinora/core gen:api` — regenerate the typed API client after a backend contract change
- Full monorepo check (the CI `apps` job): `pnpm install && pnpm run typecheck && pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/mobile typecheck && pnpm --filter @kinora/core test && pnpm --filter @kinora/desktop build`

## Running it (full detail in the README)
1. **Backend:** `cp .env.example backend/.env` (set `DASHSCOPE_API_KEY`), then `make stack-up` (docker compose: postgres+pgvector, redis, minio, migrate, api, render-worker, mcp). `make seed-demo` loads a demo book.
2. **Desktop:** `make app-install` then `make app-desktop-dev` — talks to the API at `http://localhost:8000` (override with `VITE_KINORA_API_URL`).
3. **Mobile:** `make app-mobile-start` — set the API base in `apps/mobile/src/lib/config.ts` (a device/simulator can't reach `localhost`).

## The backend process model (every role = the same image, a different command; `infra/docker-compose.yml`)
| Service | Command | Role |
|---|---|---|
| `api` | `uvicorn app.main:app` | REST + SSE/WS; **runs the Scheduler in-process** + the idle-sweeper; spawns Phase-A ingest on PDF upload |
| `render-worker` | `python -m app.queue.worker` | drains the Redis priority queue; per-shot pipeline / ffmpeg degradation ladder |
| `mcp` | `python -m app.mcp.run --http` | the canon-memory MCP server |
| `migrate` | `alembic upgrade head` | one-shot schema apply |

There is **no** standalone scheduler/ingest process — both run inside `api`.

## Architecture
- **Composition root** `backend/app/composition.py` builds the wired `Container` (every DI seam). Lazy — `create_app()` + `/health` work with `DASHSCOPE_API_KEY=test` and no network.
- **Agent crew** (`backend/app/agents/`): six contract-bound agents behind `BaseAgent`; deterministic policy (render-mode tree §9.3, Critic routing §9.5, arbitration §7.2) lives in the concrete agents as pure functions.
- **Memory = the MCP canon server** (`backend/app/memory/` + `mcp/`): `MemoryTools.dispatch` is the single execution path for every tool.
- **Scheduler** (`backend/app/scheduler/`): dual-watermark buffer; committed/speculative/cold zones; reserves video-seconds from the budget.
- **Render** (`backend/app/render/`): the §9.7 per-shot state machine; the ffmpeg Ken-Burns degradation lane is a real product feature, not a placeholder.
- **Apps share `@kinora/core`:** the framework-agnostic `SyncEngine` (subscribe/getSnapshot for `useSyncExternalStore`) is the client playhead single-source-of-truth, consumed by both shells.

## Critical gotchas
- **`KINORA_LIVE_VIDEO` is OFF by default and must stay off** unless you intend to spend real Wan credits. The whole loop still runs end-to-end (Ken-Burns mp4s, budget stays 0).
- **Backend tests:** the unit suite runs with no infra (infra-bound tests skip); integration needs `KINORA_TEST_DATABASE_URL` / `_REDIS_URL` / `_S3_ENDPOINT_URL`; live model smokes are gated by `KINORA_LIVE_TESTS=1`.
- **pnpm:** `node-linker=hoisted` (RN/Metro needs it); native build-script approvals (esbuild, electron) live in `pnpm-workspace.yaml` under `allowBuilds`.
- **Config:** all backend settings in `backend/app/core/config.py` (pydantic-settings); only `DASHSCOPE_API_KEY` is required. `alembic.ini` reads the URL from `Settings`.
