# syntax=docker/dockerfile:1
#
# Production web renderer image for the Kinora desktop/Vite app. Electron stays
# the primary desktop product; this image serves the same renderer for judges in
# a normal browser, pointed at the deployed FastAPI backend.

FROM node:22-alpine AS build

WORKDIR /app
RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/desktop/package.json apps/desktop/package.json
RUN pnpm install --filter @kinora/desktop... --frozen-lockfile

COPY apps/desktop apps/desktop
ARG VITE_KINORA_API_URL=http://localhost:8000
ENV VITE_KINORA_API_URL=${VITE_KINORA_API_URL}
RUN pnpm --filter @kinora/desktop run build

FROM nginx:1.27-alpine AS runtime

COPY infra/docker/desktop.nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/desktop/dist /usr/share/nginx/html

EXPOSE 80
