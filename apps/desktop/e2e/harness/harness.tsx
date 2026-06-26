import React, { useEffect } from "react";
import ReactDOM from "react-dom/client";
import { A11yProvider } from "@/a11y/A11yProvider";
import { ReadingControls } from "@/reading/ReadingControls";
import { ReadAloudView } from "@/a11y/ReadAloudView";
import { useReadingPrefs } from "@/a11y/readingPrefs";
import { registerShortcut } from "@/a11y/keyboard";
import "@/index.css";
import "@/styles/a11y.css";

// Dev-only page (not part of the production build) that mounts Agent 06's owned
// surfaces against the real app styling, so Playwright + axe can scan them and a
// keyboard-only walkthrough can be recorded without the backend.

const SAMPLE =
  "Call me Ishmael. Some years ago—never mind how long precisely—having little or no money in my purse, " +
  "and nothing particular to interest me on shore, I thought I would sail about a little and see the watery part of the world.";

function Harness() {
  const { prefs, update } = useReadingPrefs();
  useEffect(
    () => registerShortcut("mod+,", () => {}, { description: "Open settings", scope: "Global" }),
    [],
  );
  return (
    <main id="kinora-main" style={{ padding: 24, maxWidth: 880, margin: "0 auto", display: "grid", gap: 28 }}>
      <h1 style={{ fontSize: 22, margin: 0 }}>Kinora accessibility harness</h1>
      <section aria-labelledby="rc-heading">
        <h2 id="rc-heading" style={{ fontSize: 15 }}>Reading controls</h2>
        <div style={{ background: "#15120e", padding: 20, borderRadius: 16, maxWidth: 360, border: "1px solid rgba(255,255,255,0.08)" }}>
          <ReadingControls prefs={prefs} onChange={update} />
        </div>
      </section>
      <section aria-labelledby="ra-heading">
        <h2 id="ra-heading" style={{ fontSize: 15 }}>Read aloud</h2>
        <ReadAloudView text={SAMPLE} />
      </section>
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <A11yProvider>
      <Harness />
    </A11yProvider>
  </React.StrictMode>,
);
