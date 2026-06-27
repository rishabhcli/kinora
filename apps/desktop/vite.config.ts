import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// Agent 07 (optimization): behavior-preserving build tuning for the Electron renderer.
// - target "esnext": the production renderer only ever runs in Electron 33's Chromium (~130),
//   which supports modern syntax natively, so esbuild need not down-level (smaller, faster code).
//   Dev (`vite` serve) is unaffected — this only changes `vite build`.
// - modulePreload.polyfill false: Chromium supports <link rel="modulepreload"> natively, so the
//   injected polyfill is dead weight here.
// - manualChunks: keep react/framer-motion in long-lived vendor chunks (good cache hit rate); the
//   7 page screens are already React.lazy split. See coordination/PERF.md for measured bundle deltas.
export default defineConfig(({ command }) => ({
  // base "./" for `vite build`: Electron loads the renderer via loadFile() over file://,
  // where Vite's default absolute base ("/assets/…") 404s and the app renders blank.
  // A relative base resolves assets next to index.html. Dev (served over http) keeps "/".
  base: command === "build" ? "./" : "/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
  },
  build: {
    target: "esnext",
    cssMinify: "lightningcss",
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        manualChunks: {
          "react-vendor": ["react", "react-dom"],
          "motion-vendor": ["framer-motion"],
        },
      },
    },
  },
}));
