# Kinora developer Makefile
# Backend lives in backend/ with its venv at backend/.venv; frontend in frontend/.

PYTHON ?= python3
REV_MSG ?= change

.PHONY: help install up down migrate revision lint fmt test \
        fe-install fe-dev fe-build fe-test

help:
	@echo "Kinora make targets:"
	@echo "  install     create backend/.venv and pip install -e .[dev]"
	@echo "  up / down   docker compose up -d / down (infra/)"
	@echo "  migrate     alembic upgrade head"
	@echo "  revision    alembic revision --autogenerate (REV_MSG=...)"
	@echo "  lint        ruff check + mypy"
	@echo "  fmt         black + ruff --fix"
	@echo "  test        pytest"
	@echo "  fe-install / fe-dev / fe-build / fe-test  frontend npm tasks"

install:
	cd backend && $(PYTHON) -m venv .venv && \
		.venv/bin/pip install --upgrade pip && \
		.venv/bin/pip install -e ".[dev]"

up:
	cd infra && docker compose up -d

down:
	cd infra && docker compose down

migrate:
	cd backend && .venv/bin/alembic -c alembic.ini upgrade head

revision:
	cd backend && .venv/bin/alembic -c alembic.ini revision --autogenerate -m "$(REV_MSG)"

lint:
	cd backend && .venv/bin/ruff check app tests
	cd backend && .venv/bin/mypy app

fmt:
	cd backend && .venv/bin/black app tests
	cd backend && .venv/bin/ruff check --fix app tests

test:
	cd backend && .venv/bin/pytest -q

fe-install:
	cd frontend && npm install

fe-dev:
	cd frontend && npm run dev

fe-build:
	cd frontend && npm run build

fe-test:
	cd frontend && npm test
