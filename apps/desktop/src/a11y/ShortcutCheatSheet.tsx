import { useEffect, useRef, type CSSProperties } from "react";
import { getRegisteredShortcuts, prettyCombo, type RegisteredShortcut } from "./keyboard";
import { trapFocus, restoreFocus } from "./focus";

// The `?` cheat-sheet: a focus-trapped, Escape-closable dialog listing every
// registered shortcut, grouped by scope. Discoverability for the keyboard layer.

const kbdStyle: CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: "0.8rem",
  padding: "0.15rem 0.5rem",
  borderRadius: 6,
  background: "rgba(255,255,255,0.08)",
  border: "1px solid rgba(255,255,255,0.14)",
  whiteSpace: "nowrap",
  marginLeft: "1rem",
};

export interface ShortcutCheatSheetProps {
  open: boolean;
  onClose: () => void;
}

export function ShortcutCheatSheet({ open, onClose }: ShortcutCheatSheetProps) {
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
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      release();
      restoreFocus(restoreRef.current);
    };
  }, [open, onClose]);

  if (!open) return null;

  // De-dupe identical combos (StrictMode double-registration) then group by scope.
  const seen = new Set<string>();
  const shortcuts = getRegisteredShortcuts().filter((s) => {
    if (!s.description) return false;
    const key = `${s.scope ?? ""}|${s.combo}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const groups = new Map<string, RegisteredShortcut[]>();
  for (const s of shortcuts) {
    const scope = s.scope ?? "General";
    if (!groups.has(scope)) groups.set(scope, []);
    groups.get(scope)!.push(s);
  }

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        display: "grid",
        placeItems: "center",
        background: "rgba(0,0,0,0.55)",
        padding: "1rem",
      }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="a11y-cheatsheet-title"
        tabIndex={-1}
        style={{
          maxWidth: 540,
          width: "100%",
          maxHeight: "80vh",
          overflowY: "auto",
          padding: "1.5rem",
          borderRadius: 18,
          background: "#15120e",
          color: "#e8e2d8",
          border: "1px solid rgba(255,255,255,0.1)",
          boxShadow: "0 24px 80px rgba(0,0,0,0.6)",
          outline: "none",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "1rem",
          }}
        >
          <h2 id="a11y-cheatsheet-title" style={{ margin: 0, fontSize: "1.15rem" }}>
            Keyboard shortcuts
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close keyboard shortcuts"
            style={{
              background: "rgba(255,255,255,0.06)",
              border: "1px solid rgba(255,255,255,0.14)",
              color: "inherit",
              borderRadius: 8,
              padding: "0.3rem 0.7rem",
              cursor: "pointer",
            }}
          >
            Close
          </button>
        </div>

        {shortcuts.length === 0 ? (
          <p style={{ opacity: 0.7 }}>No shortcuts registered yet.</p>
        ) : (
          Array.from(groups.entries()).map(([scope, list]) => (
            <section key={scope} aria-label={scope} style={{ marginBottom: "1rem" }}>
              <h3
                style={{
                  margin: "0 0 0.4rem",
                  fontSize: "0.72rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.08em",
                  opacity: 0.55,
                }}
              >
                {scope}
              </h3>
              <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                {list.map((s) => (
                  <li
                    key={s.combo}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      padding: "0.4rem 0",
                      borderBottom: "1px solid rgba(255,255,255,0.06)",
                    }}
                  >
                    <span>{s.description}</span>
                    <kbd style={kbdStyle}>{prettyCombo(s.combo)}</kbd>
                  </li>
                ))}
              </ul>
            </section>
          ))
        )}
      </div>
    </div>
  );
}
