# syntax=docker/dockerfile:1.7
#
# Production web renderer image for the Kinora desktop/Vite app. Electron stays
# the primary desktop product; this image serves the same renderer for judges in
# a normal browser, pointed at the deployed FastAPI backend.

# ---------------------------------------------------------------------------- #
# Stage 1: build the Vite bundle. A BuildKit cache mount keeps the pnpm store
# warm across rebuilds; the install layer is split from the source copy so a code
# change doesn't re-resolve dependencies.
# ---------------------------------------------------------------------------- #
FROM node:22-alpine AS build

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
WORKDIR /app
RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/desktop/package.json apps/desktop/package.json
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --filter @kinora/desktop... --frozen-lockfile

COPY apps/desktop apps/desktop
ARG VITE_KINORA_API_URL=http://localhost:8000
ENV VITE_KINORA_API_URL=${VITE_KINORA_API_URL}
RUN pnpm --filter @kinora/desktop run build

# ---------------------------------------------------------------------------- #
# Stage 2: Nginx runtime serving the static bundle (listen 80; see nginx.conf).
# ---------------------------------------------------------------------------- #
FROM nginx:1.27-alpine AS runtime

# Drop the default site, ship ours, ship the build. The bundle is owned by root
# and served read-only by the nginx worker (which already drops to nginx:nginx).
COPY infra/docker/desktop.nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/desktop/dist /usr/share/nginx/html

EXPOSE 80

# A lightweight liveness check (frontend has no app backend; just serve the SPA).
# Use 127.0.0.1, not localhost: this image's /etc/hosts resolves localhost to
# ::1 first, but nginx only binds 0.0.0.0:80 (IPv4), so "localhost" resolved
# unhealthy for hours while the service was actually fine (verified 2026-07-04).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:80/ >/dev/null 2>&1 || exit 1
