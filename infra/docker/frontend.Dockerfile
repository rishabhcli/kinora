# syntax=docker/dockerfile:1
#
# Kinora frontend image. Builds the Vite app and serves the production preview.
# Build context is the repo root (see infra/docker-compose.yml).
# (Recommend adding a frontend/.dockerignore for node_modules/dist in a later pass.)

FROM node:20-alpine

WORKDIR /app

# Install dependencies first for better layer caching.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install

# App source + production build.
COPY frontend/ ./
RUN npm run build

EXPOSE 5173
CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "5173"]
