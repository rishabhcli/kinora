# syntax=docker/dockerfile:1
#
# Kinora frontend image (multi-stage):
#   1) reproducible Vite build with `npm ci` against the committed package-lock.json
#   2) served by nginx as a real static server (NOT `vite preview`) with an SPA
#      history fallback and an /api reverse proxy (REST + SSE + WebSocket) so the
#      browser talks to one origin and `text/event-stream` / ws:// both work.
# Build context is the repo root (see infra/docker-compose.yml).
# (Recommend adding a frontend/.dockerignore for node_modules/dist in a later pass.)

# --- Stage 1: reproducible production build ---
FROM node:20-alpine AS build

WORKDIR /app

# Bring in the frontend source. NOTE: there is no frontend/.dockerignore yet, so
# this can also pull a host-built node_modules (wrong CPU/platform binaries). The
# `npm ci` below removes any pre-existing node_modules and installs a clean Linux
# tree from the committed lockfile — so the build is reproducible and correct
# regardless of the build host. (Add a frontend/.dockerignore for node_modules +
# dist in a later pass to also shrink the build context and restore deps-layer
# caching.)
COPY frontend/ ./

# `npm ci` installs EXACTLY the committed package-lock.json (and fails if it is
# out of sync with package.json) — the reproducible, CI-safe install, unlike the
# previous `npm install`. Then the production build -> /app/dist (static assets).
RUN npm ci && npm run build

# --- Stage 2: static server (nginx) ---
FROM nginx:alpine AS runtime

# Upstream the /api proxy targets. Defaults to the compose service name; override
# (e.g. an internal LB or the api node's private addr) for other deployments.
# Only this var is substituted into the template (NGINX_ENVSUBST_FILTER) so nginx
# runtime vars like $host / $uri / $http_upgrade are left intact by envsubst.
ENV API_UPSTREAM=api:8000 \
    NGINX_ENVSUBST_FILTER=^API_UPSTREAM$

# Static build output.
COPY --from=build /app/dist /usr/share/nginx/html

# WebSocket upgrade map (http context, included via conf.d). Kept as a plain
# conf.d file (NOT a template) so envsubst never touches $http_upgrade.
COPY <<'MAP' /etc/nginx/conf.d/00-upgrade-map.conf
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
MAP

# Server config rendered by the nginx:alpine entrypoint (envsubst on *.template).
# Overwrites the image's default.conf so only this :5173 server remains.
COPY <<'NGINX' /etc/nginx/templates/default.conf.template
server {
    listen       5173;
    listen       [::]:5173;
    server_name  _;

    root  /usr/share/nginx/html;
    index index.html;

    # SPA history fallback: serve index.html for client-side routes.
    location / {
        try_files $uri $uri/ /index.html;
    }

    # Reverse-proxy the backend. The frontend calls everything under /api
    # (REST + the SSE event stream + the Director WebSocket), so this one block
    # covers plain HTTP, text/event-stream, and the ws:// upgrade.
    location /api/ {
        proxy_pass http://${API_UPSTREAM};
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket upgrade (Director WS at /api/ws/...).
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # SSE: stream events unbuffered, no read timeout.
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 1h;
    }
}
NGINX

EXPOSE 5173
# Default nginx:alpine CMD runs the entrypoint (renders the template) then
# `nginx -g 'daemon off;'`.
