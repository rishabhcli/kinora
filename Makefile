# Kinora developer Makefile
# Backend lives in backend/ with its venv at backend/.venv. The Electron desktop
# app lives in apps/desktop (pnpm + Turborepo). Infra lives in infra/.

PYTHON ?= python3
REV_MSG ?= change
SEED_ARGS ?=
MCP_ARGS ?= --http

.PHONY: help install up down stack-up stack-down migrate revision \
        worker ingest-worker mcp provider-preflight demo-pdf seed-demo lint fmt test \
        verify-models \
        app-install app-typecheck app-test app-desktop-dev app-desktop-build \
        app-native app-native-bundle

help:
	@echo "Kinora make targets:"
	@echo "  install      create backend/.venv and pip install -e .[dev]"
	@echo "  stack-up     docker compose build + up -d (data plane, migrate, api, workers, mcp)"
	@echo "  stack-down   docker compose down (infra/)"
	@echo "  up / down    docker compose up -d / down (infra/) [aliases]"
	@echo "  migrate      alembic upgrade head (local venv against \$$DATABASE_URL)"
	@echo "  revision     alembic revision --autogenerate (REV_MSG=...)"
	@echo "  worker       run the render worker locally (python -m app.queue.worker)"
	@echo "  ingest-worker run the ingest recovery worker locally"
	@echo "  mcp          run the MCP canon-memory server (MCP_ARGS=--http by default)"
	@echo "  provider-preflight safe Qwen/DashScope provider checks (PREFLIGHT_ARGS=...)"
	@echo "  demo-pdf     (re)build the bundled public-domain demo book PDF"
	@echo "  seed-demo    load the demo book via the real flow (SEED_ARGS=... e.g. '--via direct')"
	@echo "  lint         ruff check + mypy"
	@echo "  fmt          black + ruff --fix"
	@echo "  test         pytest"
	@echo "  verify-models  run the explicit-state model checker over the protocol specs (§4.5/§7.2/§12)"
	@echo "  app-install / app-typecheck / app-test                   desktop app (pnpm)"
	@echo "  app-desktop-dev / app-desktop-build                      run/build Electron"
	@echo "  app-native        run the native macOS Liquid Glass shell (needs app-desktop-dev for :5173)"
	@echo "  app-native-bundle build + open KinoraGlass.app (real .app bundle)"

install:
	cd backend && $(PYTHON) -m venv .venv && \
		.venv/bin/pip install --upgrade pip && \
		.venv/bin/pip install -e ".[dev]"

# -- Docker Compose stack (the real process model, infra/docker-compose.yml) -- #

stack-up:
	cd infra && docker compose up -d --build

stack-down:
	cd infra && docker compose down

up:
	cd infra && docker compose up -d

down:
	cd infra && docker compose down

# -- Database migrations ----------------------------------------------------- #

migrate:
	cd backend && .venv/bin/alembic -c alembic.ini upgrade head

revision:
	cd backend && .venv/bin/alembic -c alembic.ini revision --autogenerate -m "$(REV_MSG)"

# -- Real process model (run locally from the venv) -------------------------- #

worker:
	cd backend && .venv/bin/python -m app.queue.worker

ingest-worker:
	cd backend && .venv/bin/python -m app.ingest.recovery

mcp:
	cd backend && .venv/bin/python -m app.mcp.run $(MCP_ARGS)

provider-preflight:
	cd backend && .venv/bin/python scripts/provider_preflight.py $(PREFLIGHT_ARGS)

# -- Demo content ------------------------------------------------------------ #

demo-pdf:
	backend/.venv/bin/python assets/books/build_demo_pdf.py

seed-demo:
	cd backend && .venv/bin/python scripts/seed_demo.py $(SEED_ARGS)

# -- Quality gates ----------------------------------------------------------- #

lint:
	cd backend && .venv/bin/ruff check app tests scripts
	cd backend && .venv/bin/mypy app tests

fmt:
	cd backend && .venv/bin/black app tests
	cd backend && .venv/bin/ruff check --fix app tests

test:
	cd backend && .venv/bin/pytest -q

verify-models:
	cd backend && .venv/bin/python -m app.verification.run

# -- Apps (Electron desktop + browser renderer, pnpm + Turborepo) ----------- #

app-install:
	pnpm install

app-typecheck:
	pnpm --filter @kinora/desktop run typecheck

app-test:
	pnpm --filter @kinora/desktop run test

app-desktop-dev:
	pnpm --filter @kinora/desktop run dev

app-desktop-build:
	pnpm --filter @kinora/desktop run build

# -- Native macOS shell (real Liquid Glass; SwiftPM, needs the macOS 26+ SDK) - #
# Hosts the renderer in a WKWebView. Requires the renderer dev server on :5173,
# so run `make app-desktop-dev` in another shell first for live development.

app-native:
	DEVELOPER_DIR="$(shell ls -d /Applications/Xcode*.app 2>/dev/null | head -1)/Contents/Developer" swift run --package-path apps/desktop-native KinoraGlass

app-native-bundle:
	bash apps/desktop-native/build-app.sh
	open apps/desktop-native/KinoraGlass.app
