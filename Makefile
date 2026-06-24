# Kinora developer Makefile
# Backend lives in backend/ with its venv at backend/.venv. The Electron desktop
# and Expo mobile apps live in apps/ (pnpm + Turborepo). Infra in infra/.

PYTHON ?= python3
REV_MSG ?= change
SEED_ARGS ?=
MCP_ARGS ?= --http

.PHONY: help install up down stack-up stack-down migrate revision \
        worker mcp demo-pdf seed-demo seed-demo-books lint fmt test \
        app-install app-typecheck app-test app-desktop-dev app-desktop-build app-mobile-start \
        app-native app-native-bundle

help:
	@echo "Kinora make targets:"
	@echo "  install      create backend/.venv and pip install -e .[dev]"
	@echo "  stack-up     docker compose build + up -d (data plane, migrate, api, worker, mcp)"
	@echo "  stack-down   docker compose down (infra/)"
	@echo "  up / down    docker compose up -d / down (infra/) [aliases]"
	@echo "  migrate      alembic upgrade head (local venv against \$$DATABASE_URL)"
	@echo "  revision     alembic revision --autogenerate (REV_MSG=...)"
	@echo "  worker       run the render worker locally (python -m app.queue.worker)"
	@echo "  mcp          run the MCP canon-memory server (MCP_ARGS=--http by default)"
	@echo "  demo-pdf     (re)build the bundled public-domain demo book PDFs"
	@echo "  seed-demo    load one demo book via the real flow (SEED_ARGS=... e.g. '--via direct')"
	@echo "  seed-demo-books  load both Frog-King + Little Red Riding Hood (SEED_ARGS=...)"
	@echo "  lint         ruff check + mypy"
	@echo "  fmt          black + ruff --fix"
	@echo "  test         pytest"
	@echo "  app-install / app-typecheck / app-test                   apps monorepo (pnpm)"
	@echo "  app-desktop-dev / app-desktop-build / app-mobile-start   run the apps"
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

mcp:
	cd backend && .venv/bin/python -m app.mcp.run $(MCP_ARGS)

# -- Demo content ------------------------------------------------------------ #

demo-pdf:
	backend/.venv/bin/python assets/books/build_demo_pdf.py
	backend/.venv/bin/python assets/books/build_little_red_riding_hood_pdf.py

seed-demo:
	cd backend && .venv/bin/python scripts/seed_demo.py $(SEED_ARGS)

seed-demo-books:
	cd backend && .venv/bin/python scripts/seed_demo_books.py $(SEED_ARGS)

# -- Quality gates ----------------------------------------------------------- #

lint:
	cd backend && .venv/bin/ruff check app tests scripts
	cd backend && .venv/bin/mypy app tests

fmt:
	cd backend && .venv/bin/black app tests
	cd backend && .venv/bin/ruff check --fix app tests

test:
	cd backend && .venv/bin/pytest -q

# -- Apps (Electron desktop + Expo mobile, pnpm + Turborepo) ----------------- #

app-install:
	pnpm install

app-typecheck:
	pnpm run typecheck
	pnpm --filter @kinora/desktop run typecheck
	pnpm --filter @kinora/mobile run typecheck

app-test:
	pnpm --filter @kinora/core run test

app-desktop-dev:
	pnpm --filter @kinora/desktop run dev

app-desktop-build:
	pnpm --filter @kinora/desktop run build

app-mobile-start:
	pnpm --filter @kinora/mobile run start

# -- Native macOS shell (real Liquid Glass; SwiftPM, needs the macOS 26+ SDK) - #
# Hosts the renderer in a WKWebView. Requires the renderer dev server on :5173,
# so run `make app-desktop-dev` in another shell first for live development.

app-native:
	swift run --package-path apps/desktop-native

app-native-bundle:
	bash apps/desktop-native/build-app.sh
	open apps/desktop-native/KinoraGlass.app
