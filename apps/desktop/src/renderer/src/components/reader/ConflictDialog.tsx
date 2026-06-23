import {
  type ConflictActivity,
  type ConflictOption,
  conflictOptionLabel,
  type ConflictTrace,
} from "@kinora/core";
import { useEffect, useRef, useState } from "react";

interface ConflictDialogProps {
  /** The surfaced §7.2 conflict, or null when the crew is in agreement. */
  conflict: ConflictActivity | null;
  /** The streamed resolution (Showrunner reasoning + resolved flag). */
  trace: ConflictTrace;
  /** The disputed shot's current clip, shown as the "frame in question". */
  shotClipUrl: string | null;
  onResolve: (conflictId: string, option: string) => void;
  onDismiss: (conflictId: string) => void;
}

/** Order the options so the safe default (honour) leads and the rarer evolve trails. */
const OPTION_ORDER: Record<string, number> = { honor_canon: 0, evolve_canon: 1, surface_to_user: 2 };

function OptionIcon({ id }: { id: string }) {
  if (id === "evolve_canon") {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 3v4M12 17v4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M3 12h4M17 12h4M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8" />
      </svg>
    );
  }
  if (id === "surface_to_user") {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="8" r="4" />
        <path d="M4 20c0-4 4-6 8-6s8 2 8 6" />
      </svg>
    );
  }
  // honor_canon (and the safe default) — a shield.
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3Z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

/** A short cost/precondition caption for one option. */
function optionCaption(option: ConflictOption): string {
  if (option.requires) return `needs ${option.requires}`;
  if (option.cost_video_s && option.cost_video_s > 0) return `+${Math.round(option.cost_video_s)}s render`;
  if (option.cost_video_s === 0) return "no new render";
  return "";
}

/**
 * The Crew-dispute modal — the §7.2 "money shot". When the Continuity Supervisor
 * flags a canon violation the Showrunner can't auto-resolve, the dispute surfaces
 * here: the frame in question, the canon fact it contradicts, and the three
 * policy options. The Director picks; the Showrunner's arbitration then streams in
 * and the affected shot regenerates (or canon evolves) per the choice.
 */
export function ConflictDialog({
  conflict,
  trace,
  shotClipUrl,
  onResolve,
  onDismiss,
}: ConflictDialogProps) {
  const [picked, setPicked] = useState<string | null>(null);
  const firstOptionRef = useRef<HTMLButtonElement | null>(null);

  const conflictId = conflict?.conflictId ?? null;
  // Reset the optimistic pick whenever a *new* conflict surfaces.
  useEffect(() => {
    setPicked(null);
  }, [conflictId]);

  // Esc dismisses (the dispute persists in the feed; this just closes the modal).
  useEffect(() => {
    if (!conflict) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onDismiss(conflict.conflictId);
    };
    window.addEventListener("keydown", onKey);
    firstOptionRef.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [conflict, onDismiss]);

  if (!conflict) return null;

  const chosen = picked ?? trace.chosen;
  const phase: "options" | "arbitrating" | "resolved" = trace.resolved
    ? "resolved"
    : chosen
      ? "arbitrating"
      : "options";

  const options = [...conflict.options].sort(
    (a, b) => (OPTION_ORDER[a.id] ?? 9) - (OPTION_ORDER[b.id] ?? 9),
  );

  const choose = (option: string): void => {
    setPicked(option);
    onResolve(conflict.conflictId, option);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-6"
      role="dialog"
      aria-modal="true"
      aria-label="Crew dispute — continuity conflict"
    >
      <div
        className="absolute inset-0 bg-black/55 backdrop-blur-sm motion-safe:animate-[fadeIn_0.2s_ease-out]"
        onClick={() => onDismiss(conflict.conflictId)}
      />

      <div className="glass-strong relative flex w-full max-w-lg flex-col overflow-hidden rounded-glass border border-white/10 shadow-2xl">
        {/* A thin rose seam reads as "the crew is in disagreement". */}
        <div className="h-1 w-full bg-gradient-to-r from-rose-500/70 via-rose-400/40 to-transparent" />

        <div className="flex flex-col gap-4 p-6">
          <header className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-rose-300">
                <span className="relative flex h-1.5 w-1.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-rose-400/70 motion-reduce:hidden" />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-rose-400" />
                </span>
                Crew dispute · §7.2
              </p>
              <h2 className="mt-1 font-display text-[18px] leading-tight text-parchment">
                {conflict.raisedBy?.includes("continuity")
                  ? "Continuity Supervisor flagged a canon violation"
                  : "A canon violation needs the Director"}
              </h2>
            </div>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => onDismiss(conflict.conflictId)}
              className="-mr-1 -mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-white/50 transition hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="m6 6 12 12M18 6 6 18" /></svg>
            </button>
          </header>

          {/* The frame in question + the canon it contradicts. */}
          <div className="flex gap-4">
            <div className="relative aspect-video w-40 shrink-0 overflow-hidden rounded-xl bg-black ring-1 ring-white/10">
              {shotClipUrl ? (
                <video
                  className="h-full w-full object-cover"
                  src={`${shotClipUrl}#t=0.5`}
                  preload="metadata"
                  muted
                  playsInline
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center bg-[radial-gradient(120%_100%_at_50%_0%,#241812,#0b0705)] text-white/30">
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M4 5h16v14H4zM4 9h16M9 5 7 9M15 5l-2 4" /></svg>
                </div>
              )}
              <span className="absolute bottom-1 left-1 rounded bg-black/65 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-rose-200">
                in question
              </span>
            </div>

            <div className="min-w-0 flex-1 space-y-2.5">
              <div>
                <p className="text-[10px] font-semibold uppercase tracking-wide text-white/40">The shot depicts</p>
                <p className="text-[13px] leading-snug text-parchment">{conflict.claim ?? "a contradiction with the established canon"}</p>
              </div>
              {conflict.canonFact && (
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-wide text-white/40">But canon says</p>
                  <p className="text-[12.5px] leading-snug text-amber-200/90">{conflict.canonFact}</p>
                </div>
              )}
            </div>
          </div>

          {phase === "options" && (
            <div className="space-y-2">
              <p className="text-[12px] text-white/55">How should the crew resolve it?</p>
              <div className="grid gap-2">
                {options.map((option, i) => {
                  const primary = option.id === "honor_canon";
                  return (
                    <button
                      key={option.id}
                      ref={i === 0 ? firstOptionRef : undefined}
                      type="button"
                      onClick={() => choose(option.id)}
                      className={`group flex items-center gap-3 rounded-xl border px-3.5 py-2.5 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow ${
                        primary
                          ? "border-ember/40 bg-ember/12 hover:bg-ember/20"
                          : "border-white/10 bg-white/[0.04] hover:bg-white/[0.09]"
                      }`}
                    >
                      <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${primary ? "bg-ember/25 text-ember-glow" : "bg-white/8 text-white/65"}`}>
                        <OptionIcon id={option.id} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-[13px] font-semibold capitalize text-parchment">
                          {conflictOptionLabel(option.id)}
                        </span>
                        <span className="block truncate text-[12px] text-white/55">{option.action}</span>
                      </span>
                      {optionCaption(option) && (
                        <span className="shrink-0 rounded-full bg-black/30 px-2 py-0.5 text-[10px] font-medium text-white/60">
                          {optionCaption(option)}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {phase !== "options" && (
            <div className="rounded-xl border border-white/10 bg-black/20 p-3.5">
              <div className="flex items-center gap-2">
                {phase === "resolved" ? (
                  <span className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/20 text-emerald-300">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12 4 4L19 6" /></svg>
                  </span>
                ) : (
                  <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-ember-glow/30 border-t-ember-glow" />
                )}
                <p className="text-[12px] font-semibold text-parchment">
                  {phase === "resolved"
                    ? `Resolved — ${conflictOptionLabel(chosen)}`
                    : `Showrunner is arbitrating — ${conflictOptionLabel(chosen)}`}
                </p>
              </div>

              <ul className="mt-2.5 space-y-1.5 border-l border-white/10 pl-3">
                {trace.reasoning.length === 0 ? (
                  <li className="text-[12px] italic text-white/45">Consulting the canon graph…</li>
                ) : (
                  trace.reasoning.map((line, i) => (
                    <li key={i} className="text-[12px] leading-snug text-white/70">
                      {line}
                    </li>
                  ))
                )}
              </ul>

              {phase === "resolved" && (
                <button
                  type="button"
                  onClick={() => onDismiss(conflict.conflictId)}
                  className="mt-3 w-full rounded-full bg-white/90 py-1.5 text-[12px] font-semibold text-walnut-deep transition hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
                >
                  Back to reading
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
