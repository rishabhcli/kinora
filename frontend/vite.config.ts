import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The backend (Phase 9) serves everything under `/api`, including the SSE
// stream (`/api/sessions/:id/events`) and the Director WebSocket
// (`/api/ws/sessions/:id`). In dev we proxy all of `/api` to the FastAPI
// gateway on :8000. `ws: true` upgrades the WebSocket route; SSE works through
// the same proxy because http-proxy streams `text/event-stream` unbuffered.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": {
        target: process.env.KINORA_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  preview: {
    port: 5173,
    host: true,
  },
  test: {
    environment: "jsdom",
    css: true,
    setupFiles: ["./src/test/setup.ts"],
    // Unit tests live under src/; the Playwright e2e specs (e2e/*.spec.ts) are
    // run by `npm run e2e`, not vitest (their `test.describe` is Playwright's).
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
