import React from "react";
import ReactDOM from "react-dom/client";
import { A11yProvider } from "@/a11y/A11yProvider";
import LibraryPage from "@/components/LibraryPage";
import "@/index.css";
import "@/styles/a11y.css";

// Mounts the REAL LibraryPage (Agent 5) so axe can scan it deterministically —
// the live in-app nav switch is unreliable headless. Wrapped in <main> + the
// a11y provider so the scan reflects the integrated context.

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <A11yProvider>
      <main id="kinora-main">
        <LibraryPage />
      </main>
    </A11yProvider>
  </React.StrictMode>,
);
