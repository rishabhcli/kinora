#!/usr/bin/env bash
# Bootstrap the complete Kinora demo stack on one Alibaba Cloud Linux ECS node.
set -euo pipefail

SOURCE_REPO_URL="${SOURCE_REPO_URL:-https://github.com/rishabhcli/kinora.git}"
SOURCE_REF="${SOURCE_REF:-main}"
PUBLIC_HOST="${PUBLIC_HOST:?Set PUBLIC_HOST to the ECS public IPv4 address}"
ENV_SOURCE="${ENV_SOURCE:-/root/kinora-backend.env.source}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/kinora}"

install_docker() {
  if command -v docker >/dev/null 2>&1 \
    && systemctl list-unit-files docker.service >/dev/null 2>&1; then
    return
  fi

  dnf install -y git curl openssl dnf-plugins-core
  dnf remove -y podman-docker || true
  if ! systemctl list-unit-files docker.service >/dev/null 2>&1; then
    if [[ ! -f /etc/yum.repos.d/docker-ce.repo ]]; then
      dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    fi
    dnf install -y \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
}

install_compose() {
  if docker compose version >/dev/null 2>&1; then
    return
  fi

  local machine compose_arch
  machine="$(uname -m)"
  case "$machine" in
    aarch64 | arm64) compose_arch="aarch64" ;;
    x86_64 | amd64) compose_arch="x86_64" ;;
    *) echo "Unsupported architecture: $machine" >&2; exit 1 ;;
  esac
  install -d -m 0755 /usr/local/lib/docker/cli-plugins
  curl -fsSL \
    "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-${compose_arch}" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
}

configure_swap() {
  if ! swapon --show | grep -q .; then
    fallocate -l 4G /swapfile
    chmod 0600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
}

install_docker
systemctl enable --now docker
install_compose
configure_swap

install -d -m 0755 "$INSTALL_ROOT"
rm -rf "$INSTALL_ROOT/source"
git clone --depth 1 "$SOURCE_REPO_URL" "$INSTALL_ROOT/source"
cd "$INSTALL_ROOT/source"
git fetch --depth 1 origin "$SOURCE_REF" || true
git checkout "$SOURCE_REF"

if [[ ! -s "$ENV_SOURCE" ]]; then
  echo "Missing environment source: $ENV_SOURCE" >&2
  exit 1
fi

install -m 0600 "$ENV_SOURCE" backend/.env
{
  echo
  echo 'APP_ENV=production'
  echo 'LOG_LEVEL=INFO'
  echo 'REASONING_PROVIDER=dashscope'
  echo 'VIDEO_BACKEND=dashscope'
  echo 'VIDEO_MODEL=wan2.1-t2v-turbo'
  echo 'VIDEO_MODEL_I2V=wan2.1-i2v-turbo'
  echo 'VIDEO_MODEL_R2V=wan2.1-i2v-turbo'
  echo 'KINORA_LIVE_VIDEO=true'
  echo 'KINORA_PROD_LIVE_VIDEO_OK=1'
  echo 'BUDGET_CEILING_VIDEO_S=3000'
  echo 'BUDGET_PER_SESSION_S=600'
  echo 'BUDGET_PER_SCENE_S=120'
  echo 'BUDGET_LOW_FLOOR_S=60'
  echo 'DATABASE_URL=postgresql+asyncpg://kinora:kinora@postgres:5432/kinora'
  echo 'REDIS_URL=redis://redis:6379/0'
  echo 'S3_ENDPOINT_URL=http://minio:9000'
  echo 'S3_REGION=us-east-1'
  echo 'S3_BUCKET=kinora'
  echo 'S3_ACCESS_KEY=kinora_cloud'
  printf 'S3_SECRET_KEY=%s\n' "$(openssl rand -hex 24)"
  echo 'S3_PUBLIC_BASE_URL=/media'
  printf 'JWT_SECRET=%s\n' "$(openssl rand -hex 48)"
  printf 'API_KEY_PEPPER=%s\n' "$(openssl rand -hex 48)"
  printf 'MCP_AUTH_TOKEN=%s\n' "$(openssl rand -hex 48)"
  printf 'CORS_ORIGINS=["http://%s"]\n' "$PUBLIC_HOST"
} >> backend/.env

cat > infra/docker-compose.cloud.yml <<'YAML'
name: kinora-cloud

x-backend: &backend
  build:
    context: ..
    dockerfile: infra/docker/backend.Dockerfile
  image: kinora-backend:cloud
  env_file:
    - ../backend/.env
  networks:
    - kinora

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: kinora
      POSTGRES_PASSWORD: kinora
      POSTGRES_DB: kinora
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U kinora -d kinora"]
      interval: 5s
      timeout: 5s
      retries: 20
    restart: unless-stopped
    networks: [kinora]

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20
    restart: unless-stopped
    networks: [kinora]

  minio:
    image: minio/minio:latest
    command: ["server", "/data"]
    environment:
      MINIO_ROOT_USER: ${S3_ACCESS_KEY}
      MINIO_ROOT_PASSWORD: ${S3_SECRET_KEY}
    volumes:
      - miniodata:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 5s
    restart: unless-stopped
    networks: [kinora]

  minio-bootstrap:
    image: minio/mc:latest
    depends_on:
      minio:
        condition: service_healthy
    environment:
      S3_ACCESS_KEY: ${S3_ACCESS_KEY}
      S3_SECRET_KEY: ${S3_SECRET_KEY}
    entrypoint:
      - /bin/sh
      - -c
      - |
        set -e
        mc alias set kinora http://minio:9000 "$${S3_ACCESS_KEY}" "$${S3_SECRET_KEY}"
        mc mb --ignore-existing kinora/kinora
        mc anonymous set download kinora/kinora
    restart: "no"
    networks: [kinora]

  migrate:
    <<: *backend
    command: ["alembic", "-c", "alembic.ini", "upgrade", "head"]
    depends_on:
      postgres:
        condition: service_healthy
    restart: "no"

  api:
    <<: *backend
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      minio-bootstrap:
        condition: service_completed_successfully
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"]
      interval: 10s
      timeout: 5s
      retries: 18
      start_period: 15s
    restart: unless-stopped

  render-worker:
    <<: *backend
    command: ["python", "-m", "app.queue.worker"]
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

  ingest-worker:
    <<: *backend
    command: ["python", "-m", "app.ingest.recovery"]
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

  mcp:
    <<: *backend
    command: ["python", "-m", "app.mcp.run", "--http", "--host", "0.0.0.0", "--port", "8765"]
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

  frontend:
    build:
      context: ..
      dockerfile: infra/docker/desktop.Dockerfile
      args:
        VITE_KINORA_API_URL: ""
    image: kinora-frontend:cloud
    depends_on:
      api:
        condition: service_healthy
    ports:
      - "80:80"
    volumes:
      - ./docker/desktop.cloud.nginx.conf:/etc/nginx/conf.d/default.conf:ro
    restart: unless-stopped
    networks: [kinora]

networks:
  kinora:
    driver: bridge

volumes:
  pgdata:
  redisdata:
  miniodata:
YAML

cat > infra/docker/desktop.cloud.nginx.conf <<'NGINX'
server {
  listen 80;
  server_name _;

  root /usr/share/nginx/html;
  index index.html;

  location /api/ {
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 600s;
  }

  location = /health {
    proxy_pass http://api:8000/health;
  }

  location /media/ {
    proxy_pass http://minio:9000/kinora/;
  }

  location /assets/ {
    try_files $uri =404;
    add_header Cache-Control "public, max-age=31536000, immutable";
  }

  location / {
    try_files $uri $uri/ /index.html;
    add_header Cache-Control "no-store";
  }
}
NGINX

cd infra
docker compose --env-file ../backend/.env -f docker-compose.cloud.yml build
docker compose --env-file ../backend/.env -f docker-compose.cloud.yml up -d

for _ in $(seq 1 36); do
  if curl -fsS http://127.0.0.1/health >/dev/null; then
    break
  fi
  sleep 5
done
curl -fsS http://127.0.0.1/health >/dev/null

install -d -m 0755 -o 10001 -g 10001 "$INSTALL_ROOT/seed-assets"
docker compose --env-file ../backend/.env -f docker-compose.cloud.yml run --rm \
  -v "$INSTALL_ROOT/seed-assets:/assets" api \
  python scripts/seed_public_domain_direct.py || true

docker compose --env-file ../backend/.env -f docker-compose.cloud.yml ps
printf 'KINORA_READY_URL=http://%s/\n' "$PUBLIC_HOST"
