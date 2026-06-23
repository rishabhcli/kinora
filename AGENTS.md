# Repository Guidelines

## Project Structure & Module Organization

Kinora is a pnpm + Turborepo monorepo plus a Python backend. `backend/app` holds the FastAPI service (routes in `api/routes`, persistence in `db`, generation agents in `agents`, render logic in `render`, scheduler in `scheduler`; tests in `backend/tests`). `packages/core` is the shared TypeScript layer (the SyncEngine, the OpenAPI-typed API client, §5.6 event schemas, the auth store, query keys). `apps/desktop` is the Electron app (electron-vite + React + Tailwind); `apps/mobile` is the Expo / React Native app. Infrastructure and deploy assets live in `infra`, `deploy`, and `assets/books`. There is no web `frontend/` — it was retired in favor of the native apps.

## Build, Test, and Development Commands

- `make install` — create `backend/.venv` and install the backend with dev deps.
- `make stack-up` / `make stack-down` — build/run or stop the backend Docker Compose stack in `infra`.
- `make seed-demo` — load the bundled demo book through the real flow.
- `make lint`, `make fmt`, `make test` — Ruff/Mypy, Black/Ruff fixes, and pytest (backend).
- `make app-install` — `pnpm install` for the monorepo.
- `make app-typecheck` — typecheck `@kinora/core` + desktop + mobile.
- `make app-test` — `pnpm --filter @kinora/core test` (Vitest).
- `make app-desktop-dev` / `make app-desktop-build` — run / build the Electron app.
- `make app-mobile-start` — `expo start`.
- `pnpm --filter @kinora/core gen:api` — regenerate the typed API client from the backend OpenAPI.

## Coding Style & Naming Conventions

Python targets 3.11+ and uses Black + Ruff at a 100-character line length, fully typed (Mypy disallows untyped defs); `snake_case` modules/functions, first-party imports under `app`. TypeScript is strict ESM React: components `PascalCase`, hooks `useSomething`, stores end with `Store`, tests sit beside source as `*.test.ts(x)`.

## Testing Guidelines

Backend uses pytest (`backend/tests/test_*.py`); infra-bound tests skip cleanly when Postgres/Redis/S3 are unavailable, and live model/video tests stay gated by env (`KINORA_LIVE_TESTS`; `KINORA_LIVE_VIDEO` stays off). The shared core uses Vitest (`packages/core`). Desktop e2e is Playwright-on-Electron (`apps/desktop/e2e`); mobile e2e is Maestro (`apps/mobile/.maestro`) — both need a display / emulator, and their CI jobs are wired.

## Commit & Pull Request Guidelines

History uses short, imperative subjects, often scoped (`feat(desktop): ...`, `feat(core): ...`, `chore: ...`). Keep commits focused and mention migrations, env changes, or API-contract changes in the body. Pull requests should describe backend/app impact, list verification commands, link issues, and include screenshots for visible UI changes.

## Security & Configuration Tips

Copy `.env.example` to `backend/.env` for local work; never commit secrets. Keep `KINORA_LIVE_VIDEO` off unless intentionally testing live generation. The apps read the API base from `VITE_KINORA_API_URL` (desktop) and `apps/mobile/src/lib/config.ts` (mobile). pnpm uses `node-linker=hoisted`; native build-script approvals live in `pnpm-workspace.yaml` under `allowBuilds`.
