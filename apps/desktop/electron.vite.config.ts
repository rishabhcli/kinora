import react from "@vitejs/plugin-react";
import { defineConfig, externalizeDepsPlugin } from "electron-vite";

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
  },
  renderer: {
    // @kinora/core is a workspace TS package; Vite transpiles it as source.
    // Pin the dev server to 5173 so the renderer origin matches the backend's
    // CORS allow-list (http://localhost:5173) in dev.
    server: { port: 5173, strictPort: true },
    plugins: [react()],
  },
});
