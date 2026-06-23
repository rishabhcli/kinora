# Repository Guidelines

## Project Structure & Module Organization

Kinora is split into a FastAPI backend and a Vite/React frontend. Backend source lives in `backend/app`, with routes in `api/routes`, persistence in `db`, generation agents in `agents`, render logic in `render`, scheduler logic in `scheduler`, and tests in `backend/tests`. Frontend source lives in `frontend/src`, with route pages in `routes`, shared UI in `components`, state in `stores`, API clients in `api`, sync/playback logic in `sync`, and browser tests in `frontend/e2e`. Infrastructure and deployment assets are in `infra`, `deploy`, and `assets/books`.

## Build, Test, and Development Commands

- `make install`: create `backend/.venv` and install `backend` with dev dependencies.
- `make stack-up` / `make stack-down`: build and run or stop the Docker Compose stack in `infra`.
- `make seed-demo`: load the bundled demo book through the app flow.
- `make lint`, `make fmt`, `make test`: run Ruff/Mypy, Black/Ruff fixes, and pytest for the backend.
- `make fe-install`, `make fe-dev`, `make fe-build`, `make fe-test`: install, run, build, and test the frontend.
- From `frontend`, use `npm run lint`, `npm run e2e`, and `npm run e2e:install` for ESLint and Playwright.

## Coding Style & Naming Conventions

Python targets 3.11+ and uses Black plus Ruff with a 100-character line length. Keep typed functions; Mypy disallows untyped defs. Use `snake_case` for Python modules, functions, and tests, and keep first-party imports under `app`. Frontend TypeScript is ESM/React: components use `PascalCase`, hooks use `useSomething`, stores end with `Store`, and tests sit beside source as `*.test.ts` or `*.test.tsx`.

## Testing Guidelines

Backend tests use pytest and follow `backend/tests/test_*.py`; infra-bound tests should skip cleanly when Postgres, Redis, or S3 are unavailable. Frontend unit tests use Vitest and Testing Library. End-to-end coverage uses Playwright specs in `frontend/e2e/*.spec.ts`. Keep live model/video tests gated by explicit environment variables and avoid spending provider credits in default CI.

## Commit & Pull Request Guidelines

History uses short, imperative subjects, sometimes with scopes such as `fix(frontend): ...` or review summaries like `Harden backend ...`. Keep commits focused and mention migrations, env changes, or API contract changes in the body when relevant. Pull requests should describe backend/frontend impact, list verification commands, link issues when available, and include screenshots for visible UI changes.

## Security & Configuration Tips

Copy `.env.example` to `backend/.env` for local work and never commit secrets. Keep `KINORA_LIVE_VIDEO` off unless intentionally testing live generation. Document new required environment variables in `.env.example`, Docker Compose, and CI together.
