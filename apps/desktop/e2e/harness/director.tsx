import React from "react";
import ReactDOM from "react-dom/client";
import { A11yProvider } from "@/a11y/A11yProvider";
import DirectorStudio from "@/components/director/DirectorStudio";
import type { Book } from "@/data/books";
import "@/styles/index.css";
import "@/styles/a11y.css";

// E2E harness (dev-only, not in the production build): mounts the REAL
// DirectorStudio directly so Playwright can drive its tabs, comment bar, and
// session affordances deterministically. The live in-app nav swap into the
// studio (framer AnimatePresence over the library) is unreliable headless — the
// existing app-screens.spec.ts documents the same problem for the library page —
// so we mount the component, exactly like the a11y reading/library harnesses do.
//
// The studio's data calls (director.getShots/getCanon/…) degrade gracefully when
// they 404 against the e2e API mock, so the chrome renders regardless of backend
// state. KINORA_LIVE_VIDEO is irrelevant: no session is started unless a spec
// clicks "Start session".

const BOOK: Book = {
  id: "seed-frog-king",
  title: "The Frog-King",
  author: "Brothers Grimm",
  progress: 100,
  coverColor: "#2a1810",
  coverGradient: "linear-gradient(135deg, #6b4226 0%, #3a2414 100%)",
  coverImage: "",
  textColor: "#f3e6d8",
  spineColor: "#2a1810",
  live: true,
};

function DirectorHarness() {
  const [open, setOpen] = React.useState(true);
  return (
    <main id="kinora-main">
      {open ? (
        <DirectorStudio book={BOOK} author="E2E Reviewer" onClose={() => setOpen(false)} />
      ) : (
        <button type="button" data-testid="director-closed" onClick={() => setOpen(true)}>
          Director Studio closed — reopen
        </button>
      )}
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <A11yProvider>
      <DirectorHarness />
    </A11yProvider>
  </React.StrictMode>,
);
