// CommandPalette (⌘K) — a global command launcher. Fuzzy-ranks commands
// (palette.ts), groups them into sections, and supports full keyboard control:
// ↑/↓ to move (wrapping), Enter to run, Esc to close. Focus is trapped inside
// the dialog and restored to the opener on close (a11y/focus.ts).
import { useEffect, useMemo, useRef, useState } from "react";
import {
  rankCommands,
  groupRanked,
  moveSelection,
  GROUP_LABELS,
  type Command,
} from "../../lib/discovery/palette";
import { trapFocus, restoreFocus } from "../../a11y/focus";
import { announce } from "../../a11y/announce";

interface CommandPaletteProps {
  open: boolean;
  commands: Command[];
  onClose: () => void;
  placeholder?: string;
}

export default function CommandPalette({
  open,
  commands,
  onClose,
  placeholder = "Search books, pages, actions…",
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Flat ranked list (selection index runs over this) + sectioned view.
  const ranked = useMemo(() => rankCommands(commands, query), [commands, query]);
  const sections = useMemo(() => groupRanked(ranked), [ranked]);

  // Reset + capture opener + focus the input on open.
  useEffect(() => {
    if (!open) return;
    openerRef.current = (document.activeElement as HTMLElement) ?? null;
    setQuery("");
    setSelected(0);
    const t = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(t);
  }, [open]);

  // Trap focus inside the dialog; restore to the opener on close.
  useEffect(() => {
    if (!open || !dialogRef.current) return;
    const release = trapFocus(dialogRef.current);
    return () => {
      release();
      restoreFocus(openerRef.current);
    };
  }, [open]);

  // Clamp selection when the result set shrinks.
  useEffect(() => {
    setSelected((s) => (ranked.length === 0 ? 0 : Math.min(s, ranked.length - 1)));
  }, [ranked.length]);

  // Keep the highlighted row scrolled into view.
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-cmd-index="${selected}"]`);
    el?.scrollIntoView?.({ block: "nearest" });
  }, [selected, open]);

  if (!open) return null;

  const run = (cmd: Command) => {
    onClose();
    // Defer so close/restore-focus settles before the command navigates.
    setTimeout(() => cmd.run(), 0);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => moveSelection(s, 1, ranked.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => moveSelection(s, -1, ranked.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const chosen = ranked[selected];
      if (chosen) run(chosen.command);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  // Compute a flat index for each command so the sectioned rendering maps onto
  // the flat selection cursor.
  let flatIndex = -1;

  return (
    <div
      className="fixed inset-0 z-[200] flex items-start justify-center"
      style={{ background: "rgba(0,0,0,0.55)", backdropFilter: "blur(2px)", paddingTop: "12vh" }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="w-full max-w-[560px] mx-4"
        style={{
          borderRadius: 14,
          background: "rgb(var(--k-surface-raised-rgb, 26 23 20) / 0.99)",
          border: "1px solid rgba(255,255,255,0.1)",
          boxShadow: "0 28px 80px -20px rgba(0,0,0,0.75)",
          overflow: "hidden",
        }}
        onKeyDown={onKeyDown}
      >
        {/* Search input */}
        <div className="flex items-center gap-2.5 px-4 py-3" style={{ borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" style={{ color: "#c4b8aa" }} aria-hidden>
            <circle cx="11" cy="11" r="7" />
            <path d="M16.5 16.5 21 21" />
          </svg>
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls="command-palette-list"
            aria-activedescendant={ranked[selected] ? `cmd-${ranked[selected].command.id}` : undefined}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setSelected(0);
            }}
            placeholder={placeholder}
            className="flex-1 bg-transparent border-none outline-none text-[14px] text-kinora-text placeholder:text-kinora-muted"
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="text-[9px] text-kinora-muted border border-white/10 rounded px-1.5 py-0.5">esc</kbd>
        </div>

        {/* Results */}
        <div
          id="command-palette-list"
          ref={listRef}
          role="listbox"
          aria-label="Commands"
          className="max-h-[52vh] overflow-y-auto py-2"
        >
          {ranked.length === 0 ? (
            <p className="px-4 py-6 text-center text-[12px] text-kinora-muted">No results for “{query}”</p>
          ) : (
            sections.map((section) => (
              <div key={section.group} className="mb-1">
                <p className="px-4 pt-1.5 pb-1 text-[9px] font-semibold uppercase tracking-wider text-kinora-muted/70">
                  {GROUP_LABELS[section.group]}
                </p>
                {section.items.map(({ command }) => {
                  flatIndex += 1;
                  const idx = flatIndex;
                  const active = idx === selected;
                  return (
                    <button
                      key={command.id}
                      id={`cmd-${command.id}`}
                      data-cmd-index={idx}
                      role="option"
                      aria-selected={active}
                      onMouseMove={() => setSelected(idx)}
                      onClick={() => run(command)}
                      className="w-full flex items-center gap-2.5 px-4 py-2 text-left text-[12.5px] transition-colors"
                      style={{
                        background: active ? "rgba(255,255,255,0.07)" : "transparent",
                        color: active ? "var(--k-text, #e8e2d8)" : "rgba(232,226,216,0.78)",
                      }}
                    >
                      {command.icon && <span aria-hidden className="text-[13px] w-4 text-center">{command.icon}</span>}
                      <span className="flex-1 truncate">{command.title}</span>
                      {command.hint && <span className="text-[10px] text-kinora-muted">{command.hint}</span>}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

/** Imperatively announce that the palette opened (for SR users). */
export function announcePaletteOpen(): void {
  announce("Command palette opened. Type to search, arrow keys to navigate.", "polite");
}
