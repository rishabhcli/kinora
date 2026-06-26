import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";
import { A11yProvider } from "@/a11y/A11yProvider";
import { ReadingControls } from "@/reading/ReadingControls";
import { ReadAloudView } from "@/a11y/ReadAloudView";
import { useReadingPrefs } from "@/a11y/readingPrefs";
import { trapFocus, restoreFocus } from "@/a11y/focus";
import { announce } from "@/a11y/announce";
import "@/index.css";
import "@/styles/a11y.css";

// Composes Agent 06's pieces (ReadingControls + ReadAloudView + focus trap) into
// the reading panel Agent 10 will integrate, so the full keyboard-only flow
// (open book → adjust prefs → read-aloud word-sync → close) can be demonstrated
// and recorded end-to-end without the backend or a real book.

const SAMPLE =
  "Call me Ishmael. Some years ago—never mind how long precisely—having little or no money in my purse, " +
  "and nothing particular to interest me on shore, I thought I would sail about a little and see the watery part of the world.";

function ReadingPanel() {
  const { prefs, update } = useReadingPrefs();
  const [open, setOpen] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = (document.activeElement as HTMLElement) ?? null;
    const el = dialogRef.current;
    let release = () => {};
    if (el) {
      release = trapFocus(el);
      el.focus();
    }
    announce("Opened Moby-Dick");
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      release();
      restoreFocus(restoreRef.current);
    };
  }, [open]);

  return (
    <main id="kinora-main" style={{ padding: 32, maxWidth: 720, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22 }}>Reading experience (keyboard demo)</h1>
      <p style={{ opacity: 0.7 }}>Press Enter on “Open book” to begin.</p>
      <button
        type="button"
        onClick={() => setOpen(true)}
        style={{
          background: "rgba(244,201,122,0.16)",
          border: "1px solid rgba(244,201,122,0.5)",
          color: "#f4c97a",
          borderRadius: 12,
          padding: "0.6rem 1.2rem",
          cursor: "pointer",
        }}
      >
        Open book
      </button>

      {open && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.6)",
            display: "flex",
            padding: "2rem",
          }}
        >
          <div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-label="Reading Moby-Dick"
            tabIndex={-1}
            style={{
              margin: "auto",
              background: "#15120e",
              color: "#e8e2d8",
              padding: 24,
              borderRadius: 18,
              maxWidth: 920,
              width: "100%",
              maxHeight: "86vh",
              overflow: "auto",
              display: "grid",
              gridTemplateColumns: "1fr 320px",
              gap: 24,
              border: "1px solid rgba(255,255,255,0.1)",
              outline: "none",
            }}
          >
            <div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close book"
                style={{
                  background: "rgba(255,255,255,0.06)",
                  border: "1px solid rgba(255,255,255,0.16)",
                  color: "inherit",
                  borderRadius: 8,
                  padding: "0.3rem 0.8rem",
                  cursor: "pointer",
                  marginBottom: "1rem",
                }}
              >
                Close
              </button>
              <ReadAloudView text={SAMPLE} rate={prefs.ttsRate} voiceURI={prefs.ttsVoiceURI} />
            </div>
            <div style={{ background: "#1b1712", padding: 16, borderRadius: 12 }}>
              <ReadingControls prefs={prefs} onChange={update} />
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <A11yProvider>
      <ReadingPanel />
    </A11yProvider>
  </React.StrictMode>,
);
