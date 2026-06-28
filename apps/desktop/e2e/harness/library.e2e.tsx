import React from "react";
import ReactDOM from "react-dom/client";
import { A11yProvider } from "@/a11y/A11yProvider";
import LibraryPage from "@/components/LibraryPage";
// NOTE: the renderer's CSS aggregator is "@/styles/index.css" (main.tsx). The
// older a11y harness files import "@/index.css", which does not exist and 500s
// at Vite transform time — so this E2E harness uses the correct path. (Those
// older files belong to the a11y agent; we don't edit them.)
import "@/styles/index.css";
import "@/styles/a11y.css";

// E2E harness (dev-only): mounts the REAL LibraryPage standalone so Playwright
// can drive the library + upload flows deterministically — the live in-app nav
// swap into the library (framer AnimatePresence crossfade) is unreliable
// headless, exactly as app-screens.spec.ts documents.

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <A11yProvider>
      <main id="kinora-main">
        <LibraryPage />
      </main>
    </A11yProvider>
  </React.StrictMode>,
);
