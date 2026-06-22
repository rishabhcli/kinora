# syntax=docker/dockerfile:1
#
# Kinora backend image (multi-stage).
# Build context is the repo root (see infra/docker-compose.yml).

# --- Stage 1: build a self-contained virtualenv with prod deps only ---
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential covers any sdist that lacks a wheel (most deps ship wheels).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# Copy only what's needed to build the wheel for better layer caching.
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
RUN pip install --upgrade pip && pip install .

# --- Stage 2: slim runtime ---
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root runtime user.
RUN useradd --create-home --uid 10001 kinora

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Full backend source so the container can also run alembic migrations.
COPY backend/ ./
RUN chown -R kinora:kinora /app

USER kinora
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
