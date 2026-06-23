# Kinora developer Makefile
# Backend lives in backend/ with its venv at backend/.venv; frontend in frontend/.
# Infra (docker compose + terraform) lives in infra/.

PYTHON ?= python3
REV_MSG ?= change
SEED_ARGS ?=
MCP_ARGS ?= --http

.PHONY: help install up down stack-up stack-down migrate revision \
        worker mcp demo-pdf seed-demo lint fmt test \
        fe-install fe-dev fe-build fe-test

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
	@echo "  demo-pdf     (re)build the bundled public-domain demo book PDF"
	@echo "  seed-demo    load the demo book via the real flow (SEED_ARGS=... e.g. '--via direct')"
	@echo "  lint         ruff check + mypy"
	@echo "  fmt          black + ruff --fix"
	@echo "  test         pytest"
	@echo "  fe-install / fe-dev / fe-build / fe-test  frontend npm tasks"

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

# -- Frontend ---------------------------------------------------------------- #

fe-install:
	cd frontend && npm install

fe-dev:
	cd frontend && npm run dev

fe-build:
	cd frontend && npm run build

fe-test:
	cd frontend && npm test
