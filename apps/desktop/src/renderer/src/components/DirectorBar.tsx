import { type FormEvent, useState } from "react";

import type { SessionActivity } from "../hooks/useSyncEngine";

interface DirectorBarProps {
  mode: "viewer" | "director";
  onToggleMode: () => void;
  activity: SessionActivity[];
  budgetRemaining: number | null;
  onComment: (note: string) => void;
}

/**
 * The bottom director rail: viewer/director toggle, a comment box (routed to the
 * crew over the socket), a live activity ticker, and the budget indicator.
 */
export function DirectorBar({
  mode,
  onToggleMode,
  activity,
  budgetRemaining,
  onComment,
}: DirectorBarProps) {
  const [note, setNote] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = note.trim();
    if (!trimmed) return;
    onComment(trimmed);
    setNote("");
  }

  const latest = activity[0];

  return (
    <div className="flex items-center gap-3 border-t border-neutral-900 bg-neutral-950 px-4 py-2 text-xs">
      <button
        type="button"
        onClick={onToggleMode}
        className={
          mode === "director"
            ? "rounded bg-indigo-500 px-2 py-1 font-medium text-white"
            : "rounded border border-neutral-800 px-2 py-1 text-neutral-300 hover:border-neutral-600"
        }
      >
        {mode === "director" ? "Director" : "Viewer"}
      </button>

      {mode === "director" && (
        <form onSubmit={submit} className="flex flex-1 items-center gap-2">
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Direct the scene…"
            className="flex-1 rounded border border-neutral-800 bg-neutral-900 px-2 py-1 outline-none focus:border-neutral-600"
          />
          <button type="submit" className="rounded bg-indigo-500 px-2 py-1 font-medium text-white">
            Send
          </button>
        </form>
      )}

      <span className="ml-auto max-w-md truncate text-neutral-500">
        {latest ? latest.text : "crew idle"}
      </span>
      {budgetRemaining !== null && (
        <span className="shrink-0 text-amber-400">{Math.round(budgetRemaining)}s left</span>
      )}
    </div>
  );
}
