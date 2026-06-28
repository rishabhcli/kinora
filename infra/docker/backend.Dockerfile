# syntax=docker/dockerfile:1.7
#
# Kinora backend image (multi-stage, hardened). One image, many commands — the
# §process model: api / ingest-worker / render-worker / mcp / migrate all run
# this image with a different CMD (see infra/docker-compose.yml and the k8s/TF
# compute definitions).
#
# Build context is the repo root (see infra/docker-compose.yml).
# Build with BuildKit (default on modern Docker) so the cache mounts below apply.

# ---------------------------------------------------------------------------- #
# Stage 1: build a self-contained virtualenv with prod deps only.
# ---------------------------------------------------------------------------- #
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential covers any sdist that lacks a wheel (most deps ship wheels).
# BuildKit cache mounts keep apt + pip warm across rebuilds.
# hadolint ignore=DL3008
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# Copy only what's needed to build the wheel for better layer caching.
COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
# `pip install .` resolves the pinned constraints declared in pyproject.toml, so
# the package versions ARE pinned there (not loose) — DL3013 is a false positive.
# hadolint ignore=DL3013
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install .

# ---------------------------------------------------------------------------- #
# Stage 2: slim runtime.
# ---------------------------------------------------------------------------- #
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime OS deps:
#   * tini    — a real PID 1 so SIGTERM reaps cleanly (graceful worker shutdown,
#               cooperative render cancellation §12.1).
#   * ffmpeg  — the Ken-Burns degradation ladder + audio post (a real product
#               feature, §12.4). imageio-ffmpeg bundles a static binary, but a
#               system ffmpeg/ffprobe is robust insurance for the worker role.
# NOTE: no image-level HEALTHCHECK — this image is shared across roles, and an
# api-specific probe would mark the workers/mcp unhealthy. Health is owned by the
# orchestrator (compose healthchecks / k8s probes), which are role-aware.
# hadolint ignore=DL3008
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends tini ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (uid matches the k8s securityContext + the local stack).
RUN useradd --create-home --uid 10001 --user-group kinora

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Full backend source so the container can also run alembic migrations.
COPY backend/ ./
RUN chown -R kinora:kinora /app

USER kinora
EXPOSE 8000 8765

# tini reaps zombies + forwards signals so render-worker / api shut down cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
