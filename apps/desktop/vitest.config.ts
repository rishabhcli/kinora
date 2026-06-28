import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// Dedicated vitest config (does NOT extend the app vite.config.ts, which wires
// Electron + lightningcss that would interfere with jsdom tests). Mirrors only
// the `@/*` → src alias so unit tests import the same way the app does.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    // A concrete origin so jsdom exposes localStorage (the default opaque
    // origin makes localStorage throw/undefined).
    environmentOptions: { jsdom: { url: "http://localhost/" } },
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: [
      ...configDefaults.exclude,
      "src/components/icons/*.test.ts",
      "src/lib/{appearance,settings}.test.ts",
      "src/reading/__tests__/**/*.test.ts",
      "src/reading/{crossfade,fallback,machine,warmupModel}.test.ts",
      // Next-gen playback (Agent: reading-room) pure cores use node:test (DOM-free,
      // self-contained, fast); they run via run-node-tests.mjs, not jsdom/vitest.
      // Tests that import sibling *source* modules (extensionless) or touch the DOM
      // stay on vitest (e.g. webglCompositor.test.ts). See DESIGN.md.
      "src/reading/perf/{frameStats,decodeStats,observability}.test.ts",
      "src/reading/streaming/{bandwidth,qualityLadder,instrumentedFetch}.test.ts",
      "src/reading/gl/{grade,capabilities}.test.ts",
      "src/reading/scrub/{frameClock,requestVideoFrameCallback}.test.ts",
      "src/reading/offline/{swProtocol,manifest}.test.ts",
      "src/reading/gesture/*.test.ts",
      "src/reading/describedVideo/*.test.ts",
    ],
    css: false,
  },
});
