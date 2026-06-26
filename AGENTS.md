# Repository Guidelines

## ⚠️ Current reality & key decisions (read first — overrides stale notes below)

**Two desktop apps, kept deliberately separate:**
- **MAIN = `apps/desktop`** — the Electron + React + Vite + Tailwind product (auth, real backend library, vertical AI-video reading room, live API wiring). This is the **primary** app. Native window glass here = Electron `vibrancy` (macOS) / `backgroundMaterial: 'acrylic'` (Windows 11), so it stays cross-platform.
- **SECONDARY = `apps/desktop-native`** — a standalone native **SwiftUI** app whose only job is **real Apple Liquid Glass** (`.glassEffect`, macOS 26) on every control. Showcase on sample data; not backend-wired. Run with `make app-native`. Keep it separate from the Electron app.
- `packages/core` and `apps/mobile` referenced below **do not exist on disk**. The renderer's API client is `apps/desktop/src/lib/api.ts` (hand-written; there is no generated `@kinora/core`).

**Liquid Glass:** real Liquid Glass is a **native macOS 26 API only** (SwiftUI `.glassEffect` / AppKit `NSGlassEffectView`). CSS `backdrop-filter`/SVG-displacement is an imitation — **do not call it Liquid Glass.** Building the SwiftUI app needs the **Xcode toolchain** (the Command Line Tools lack the `SwiftUIMacros` plugin, so `@State`/`.glassEffect` won't compile); `make app-native` auto-sets `DEVELOPER_DIR` to the installed Xcode.

**Wan video (DashScope intl endpoint):** the model ids in `backend/app/core/config.py` were placeholders (`wan2.7-t2v` → "Model not exist"). **Working ids** (set in `backend/.env`): t2v `wan2.5-t2v-preview` or `wan2.1-t2v-turbo`; i2v `wan2.2-i2v-plus` or `wan2.1-i2v-turbo`. `wan2.2-t2v-plus` accepts the submit but **fails at render — avoid it.** The `429 Throttling.RateQuota` is on the **image** model (`qwen-image-2.0-pro`), **not** video, so t2v sidesteps it. Films are **vertical 720×1280** (short-drama). Generated clips are saved to `~/Documents/Kinora-Generated-Videos/`.

**Local infra:** host Postgres is remapped to **5433** in `infra/docker-compose.yml` (5432 collides with a non-Kinora `admitly-postgres`); in-cluster services still use `postgres:5432`. Set `S3_PUBLIC_BASE_URL=http://localhost:9000/kinora` so MinIO media URLs are browser-reachable; the client also rewrites `minio:9000`→`localhost:9000` (`toBrowserUrl` in `lib/api.ts`). Demo creds `demo@kinora.local` / `demo-password-123` own a ready book.

**UI baseline:** Kinora-aditya @ `567c502` ("UI overhaul") is the design baseline for `apps/desktop` UI reverts.

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
