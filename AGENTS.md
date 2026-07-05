# Repository Guidelines

## ⚠️ Current reality & key decisions (read first)

**Two desktop apps, kept deliberately separate:**
- **MAIN = `apps/desktop`** — the Electron + React + Vite + Tailwind product (auth, real backend library, vertical AI-video reading room, live API wiring). This is the **primary** app. Native window glass here = Electron `vibrancy` (macOS) / `backgroundMaterial: 'acrylic'` (Windows 11), so it stays cross-platform.
- **SECONDARY = `apps/desktop-native`** — a standalone native **SwiftUI** app whose only job is **real Apple Liquid Glass** (`.glassEffect`, macOS 26) on every control. Showcase on sample data; not backend-wired. Run with `make app-native`. Keep it separate from the Electron app.
- `packages/core` and `apps/mobile` referenced in older docs, CI, and Makefile targets **do not exist on disk**. The renderer's API client is `apps/desktop/src/lib/api.ts` (hand-written; there is no generated `@kinora/core`).

**Liquid Glass:** real Liquid Glass is a **native macOS 26 API only** (SwiftUI `.glassEffect` / AppKit `NSGlassEffectView`). CSS `backdrop-filter`/SVG-displacement is an imitation — **do not call it Liquid Glass.** Building the SwiftUI app needs the **Xcode toolchain** (the Command Line Tools lack the `SwiftUIMacros` plugin, so `@State`/`.glassEffect` won't compile); `make app-native` auto-sets `DEVELOPER_DIR` to the installed Xcode.

**Wan video (DashScope intl endpoint):** Kinora is hosted-Qwen/DashScope only; do **not** add a local Wan backend. Default demo ids are t2v `wan2.1-t2v-turbo`, i2v/r2v `wan2.1-i2v-turbo`. Quality overrides are t2v `wan2.5-t2v-preview` and i2v/r2v `wan2.2-i2v-plus`. `wan2.2-t2v-plus` accepts the submit but **fails at render — avoid it.** The `429 Throttling.RateQuota` is on the **image** model (`qwen-image-2.0-pro`), **not** video, so t2v sidesteps it. Films are **vertical 720×1280** (short-drama). Successful provider videos are downloaded and persisted to object storage by the render pipeline because provider task URLs expire.

**Local infra:** host Postgres is remapped to **5433** in `infra/docker-compose.yml` (5432 collides with a non-Kinora `admitly-postgres`); in-cluster services still use `postgres:5432`. Set `S3_PUBLIC_BASE_URL=http://localhost:9000/kinora` so MinIO media URLs are browser-reachable; the client also rewrites `minio:9000`→`localhost:9000` (`toBrowserUrl` in `lib/api.ts`). Demo creds `demo@kinora.local` / `demo-password-123` own a ready book.

**UI baseline:** Kinora-aditya @ `567c502` ("UI overhaul") is the design baseline for `apps/desktop` UI reverts.

## Project Structure & Module Organization

Kinora pairs a Python backend with a pnpm/Turborepo desktop workspace, and everything has a clear home. `backend/app` holds the FastAPI service: routes in `api/routes`, persistence in `db`, generation agents in `agents`, render logic in `render`, scheduler control in `scheduler`, memory/canon services in `memory`, providers in `providers`, queue workers in `queue`, and ingest in `ingest`; tests live in `backend/tests`. `apps/desktop` is the Electron + React + Vite + Tailwind product, with the hand-written backend client at `apps/desktop/src/lib/api.ts` and the reading experience under `apps/desktop/src/reading`. `apps/desktop-native` is the separate SwiftUI Liquid Glass showcase. Infrastructure and deployment assets live in `infra`, `deploy`, and `assets/books`. There is no `frontend/`, `packages/core`, or `apps/mobile` directory.

## Build, Test, and Development Commands

- `make install` — create `backend/.venv` and install the backend with dev deps.
- `make stack-up` / `make stack-down` — build/run or stop the backend Docker Compose stack in `infra`.
- `make seed-demo` — load the bundled demo book through the real flow.
- `make lint`, `make fmt`, `make test` — Ruff/Mypy, Black/Ruff fixes, and pytest (backend).
- `make app-install` — `pnpm install` for the monorepo.
- `make app-desktop-dev` / `make app-desktop-build` — run / build the Electron app.
- `pnpm --filter @kinora/desktop run typecheck` / `test` / `build` — desktop TypeScript, Vitest, and build checks.
- `make provider-preflight` — safe hosted Qwen/DashScope model preflight (add `PREFLIGHT_ARGS=--spend-smoke` only when intentionally spending tiny smoke-test credits).
- `make ingest-worker` — run the durable ingest recovery worker locally.
- `make app-native` — run the native SwiftUI Liquid Glass shell (requires Xcode and the renderer dev server on `:5173`).
- `make app-native-bundle` — build and open `apps/desktop-native/KinoraGlass.app`.
- Avoid stale targets that reference removed workspaces: `make app-mobile-start`, `pnpm --filter @kinora/core ...`, and `pnpm --filter @kinora/mobile ...`.

## Coding Style & Naming Conventions

Python targets 3.11+ and uses Black + Ruff at a 100-character line length, fully typed (Mypy disallows untyped defs); `snake_case` modules/functions, first-party imports under `app`. TypeScript is strict ESM React: components `PascalCase`, hooks `useSomething`, stores end with `Store`, tests sit beside source as `*.test.ts(x)`.

## Testing Guidelines

Backend uses pytest (`backend/tests/test_*.py`); infra-bound tests skip cleanly when Postgres/Redis/S3 are unavailable, and live model/video tests stay gated by env (`KINORA_LIVE_TESTS`; keep `KINORA_LIVE_VIDEO` off unless intentionally spending model credits). Desktop unit tests use Vitest beside source, with reading-specific scripts in `apps/desktop/src/reading`; e2e is Playwright-on-Electron under `apps/desktop/e2e` and needs a display. The native SwiftUI shell is a showcase and is not backend-wired. `.github/workflows/ci.yml` still contains stale core/mobile filters, so verify the current disk layout before trusting or editing CI.

## Commit & Pull Request Guidelines

History uses short, imperative subjects, often scoped (`feat(desktop): ...`, `feat(core): ...`, `chore: ...`). Keep commits focused and mention migrations, env changes, or API-contract changes in the body. Pull requests should describe backend/app impact, list verification commands, link issues, and include screenshots for visible UI changes.

## Security & Configuration Tips

Copy `.env.example` to `backend/.env` for local work; never commit secrets. Keep `KINORA_LIVE_VIDEO` off unless intentionally testing live generation. Set `S3_PUBLIC_BASE_URL=http://localhost:9000/kinora` for browser-reachable MinIO media in local dev. The Electron app reads the API base from `VITE_KINORA_API_URL` and defaults to `http://localhost:8000`. pnpm uses `node-linker=hoisted`; native build-script approvals live in `pnpm-workspace.yaml` under `allowBuilds`.
