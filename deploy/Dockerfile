# syntax=docker/dockerfile:1
#
# Kinora render worker image for Alibaba ECS / Function Compute (kinora.md §12.6).
# It reuses the backend image (app + venv) and adds the proof-of-deployment
# entrypoint, so the worker is the SAME code as the local render-worker.
#
# Build from the repo root, after building the backend image:
#   docker build -f infra/docker/backend.Dockerfile -t kinora-backend:local .
#   docker build -f deploy/Dockerfile --build-arg BACKEND_IMAGE=kinora-backend:local \
#       -t kinora-render-worker:local .

ARG BACKEND_IMAGE=kinora-backend:local
FROM ${BACKEND_IMAGE}

# /app already holds the backend (app/, alembic.ini, migrations/) on PYTHONPATH.
COPY deploy/alibaba_render_worker.py /app/deploy/alibaba_render_worker.py
ENV PYTHONPATH=/app

# Consume the Redis render queue and render via DashScope + OSS (gated by
# KINORA_LIVE_VIDEO). Equivalently: CMD ["python", "-m", "app.queue.worker"].
CMD ["python", "/app/deploy/alibaba_render_worker.py"]
