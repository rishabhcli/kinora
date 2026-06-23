import {
  agentRoleLabel,
  type AgentRole,
  normalizeAgentRole,
  type SessionActivity,
  shortShotId,
} from "@kinora/core";
import { type FormEvent, useRef, useState } from "react";

import type { DirectionEntry } from "../../hooks/useDirectorHistory";
import { ShotTimeline } from "./ShotTimeline";
import type { DirectorShot } from "./shots";

/** How a comment was routed — the subset of the backend `CommentResponse` we show. */
export interface CommentRoute {
  agent: string;
  aspect: string;
  message: string;
  /** Directing priors this note taught (§8.6) — shown as teach-time confirmation. */
  learned?: { label: string; applied: boolean }[];
}

interface DirectorRailProps {
  shots: DirectorShot[];
  currentShotId: string | null;
  onSeekShot: (shot: DirectorShot) => void;
  /** Playback fraction [0,1] of the shot on screen (active tile's progress bar). */
  progressFraction?: number;
  /** shotId → directions-given count (tile badges). */
  directionCounts?: Record<string, number>;
  /** Whether the shot list is still loading (timeline skeletons). */
  loadingShots?: boolean;
  /** The directions already given to the shot on screen (newest first). */
  directions?: DirectionEntry[];

  // Region select
  armed: boolean;
  canArm: boolean;
  onArmToggle: () => void;
  region: { png: string | null } | null;
  onClearRegion: () => void;

  // Comment composer
  onSend: (note: string) => Promise<CommentRoute | null>;
  sending: boolean;
  route: CommentRoute | null;

  // Live crew feed
  activity: SessionActivity[];
  budgetRemaining: number | null;
}

const ROLE_DOT: Record<AgentRole, string> = {
  showrunner: "bg-violet-400",
  adapter: "bg-teal-400",
  continuity: "bg-rose-400",
  cinematographer: "bg-ember-glow",
  generator: "bg-sky-400",
  critic: "bg-amber-400",
  unknown: "bg-white/40",
};

/** One-tap common directions — speed the reader and exercise the intent router. */
const SUGGESTIONS = ["Slower here", "Warmer light", "Wrong room", "Tighter framing", "More dramatic"];

/** One line in the live crew feed (§5.4 agent-activity / §5.6 events). */
function FeedLine({ entry }: { entry: SessionActivity }) {
  if (entry.kind === "agent") {
    const label = agentRoleLabel(entry.role);
    return (
      <li className="flex items-start gap-2">
        <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${ROLE_DOT[entry.role]}`} />
        <span className="leading-snug text-white/70">
          <span className={entry.conflict ? "font-semibold text-rose-200" : "font-medium text-white/85"}>
            {label}
            {entry.aspect ? ` · ${entry.aspect}` : ""}
          </span>{" "}
          {entry.message}
        </span>
      </li>
    );
  }
  if (entry.kind === "regen") {
    const ccs = typeof entry.qa?.ccs === "number" ? ` · CCS ${(entry.qa.ccs as number).toFixed(2)}` : "";
    return (
      <li className="flex items-start gap-2">
        <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-sky-400" />
        <span className="leading-snug text-white/70">
          Shot <span className="font-mono text-white/85">{shortShotId(entry.shotId)}</span> re-rendered{ccs}
        </span>
      </li>
    );
  }
  if (entry.kind === "conflict") {
    return (
      <li className="flex items-start gap-2">
        <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-rose-400" />
        <span className="leading-snug text-rose-200">Continuity conflict — needs a decision</span>
      </li>
    );
  }
  if (entry.kind === "budget") {
    return (
      <li className="flex items-start gap-2">
        <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" />
        <span className="leading-snug text-white/70">Budget low — {Math.round(entry.remainingS)}s of film left</span>
      </li>
    );
  }
  return (
    <li className="flex items-start gap-2">
      <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400" />
      <span className="leading-snug text-white/70">Scene stitched</span>
    </li>
  );
}

/**
 * Director mode's working surface (§5.4): the shot timeline, the region-aware
 * comment composer (with quick-direction chips, send-failure recovery, and the
 * shot's prior directions), and the live crew feed — one cohesive rail. The
 * composer binds the boxed region + note to the shot on screen and sends it
 * through the regen-triggering REST path; routing comes back as
 * "→ Cinematographer · look" and the same message logs into the feed. Routing
 * and regen completions are also announced to assistive tech.
 */
export function DirectorRail({
  shots,
  currentShotId,
  onSeekShot,
  progressFraction,
  directionCounts,
  loadingShots,
  directions,
  armed,
  canArm,
  onArmToggle,
  region,
  onClearRegion,
  onSend,
  sending,
  route,
  activity,
  budgetRemaining,
}: DirectorRailProps) {
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  async function doSend(): Promise<void> {
    const trimmed = note.trim();
    if (!trimmed || sending || !currentShotId) return;
    setError(null);
    const result = await onSend(trimmed);
    if (result) setNote("");
    else setError("Couldn’t reach the crew — your note is kept, try again.");
  }

  function onSubmit(event: FormEvent): void {
    event.preventDefault();
    void doSend();
  }

  function applySuggestion(text: string): void {
    setNote(text);
    inputRef.current?.focus();
  }

  // Politely announce routing + the newest regen completion to screen readers.
  const routeAnnounce = route
    ? `Routed to ${agentRoleLabel(normalizeAgentRole(route.agent))}${route.aspect ? `, ${route.aspect}` : ""}`
    : "";
  const latestRegen = activity.find((a) => a.kind === "regen");
  const regenAnnounce =
    latestRegen && latestRegen.kind === "regen"
      ? `Shot ${shortShotId(latestRegen.shotId)} re-rendered${
          typeof latestRegen.qa?.ccs === "number"
            ? `, consistency ${(latestRegen.qa.ccs as number).toFixed(2)}`
            : ""
        }`
      : "";

  const recent = directions ?? [];

  return (
    <div className="flex shrink-0 flex-col border-t border-white/10 bg-walnut-deep/40">
      {/* Screen-reader-only live announcements (§5.4 accessibility). */}
      <div className="sr-only" role="status" aria-live="polite">
        {routeAnnounce}
      </div>
      <div className="sr-only" role="status" aria-live="polite">
        {regenAnnounce}
      </div>

      {/* Shot timeline filmstrip */}
      <div className="border-b border-white/8">
        <ShotTimeline
          shots={shots}
          currentShotId={currentShotId}
          onSeekShot={onSeekShot}
          progressFraction={progressFraction}
          directionCounts={directionCounts}
          loading={loadingShots}
        />
      </div>

      {/* Composer */}
      <form onSubmit={onSubmit} className="flex flex-col gap-2 px-4 py-3">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onArmToggle}
            disabled={!canArm}
            aria-pressed={armed}
            className={`flex h-8 shrink-0 items-center gap-1.5 rounded-full px-3 text-[12px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow disabled:cursor-not-allowed disabled:opacity-40 ${
              armed ? "bg-ember-glow text-walnut-deep" : "bg-white/8 text-white/75 hover:bg-white/16"
            }`}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3" />
            </svg>
            {armed ? "Selecting…" : region ? "Reselect" : "Select region"}
          </button>

          {region ? (
            <div className="flex min-w-0 items-center gap-2 rounded-full bg-white/8 py-1 pl-1 pr-2.5">
              {region.png ? (
                <img
                  src={`data:image/png;base64,${region.png}`}
                  alt="Selected region"
                  className="h-6 w-6 shrink-0 rounded-full object-cover ring-1 ring-white/20"
                />
              ) : (
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-ember/25 text-ember-glow">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3" /></svg>
                </span>
              )}
              <span className="truncate text-[11px] text-white/70">Region bound to this shot</span>
              <button
                type="button"
                onClick={onClearRegion}
                aria-label="Clear selected region"
                className="shrink-0 rounded-full p-0.5 text-white/50 transition hover:text-white"
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="m6 6 12 12M18 6 6 18" /></svg>
              </button>
            </div>
          ) : (
            <span className="truncate text-[11px] text-white/35">
              {canArm ? "Box a detail, or just describe the change." : "Waiting for a frame…"}
            </span>
          )}
        </div>

        {/* Quick-direction chips (shown when the note is empty). */}
        {canArm && !note.trim() && (
          <div className="flex flex-wrap gap-1.5">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => applySuggestion(s)}
                className="rounded-full bg-white/[0.06] px-2.5 py-1 text-[11px] text-white/65 transition hover:bg-white/12 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
              >
                {s}
              </button>
            ))}
          </div>
        )}

        <div className="flex items-center gap-2">
          <input
            ref={inputRef}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void doSend();
              }
            }}
            placeholder="Direct this shot — “make her coat crimson”"
            aria-label="Direction for the selected shot"
            className="glass-input min-w-0 flex-1 rounded-full px-3.5 py-2 text-[13px]"
          />
          <button
            type="submit"
            disabled={!note.trim() || sending || !currentShotId}
            title="Send (⌘↵)"
            className="flex shrink-0 items-center gap-1.5 rounded-full bg-white/90 px-4 py-2 text-[12px] font-semibold text-walnut-deep transition hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow disabled:cursor-not-allowed disabled:opacity-40"
          >
            {sending && (
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-walnut-deep/30 border-t-walnut-deep motion-reduce:animate-none" />
            )}
            {sending ? "Routing" : "Send"}
          </button>
        </div>

        {error && (
          <div className="flex items-center gap-2 text-[11.5px] text-rose-300">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" /></svg>
            <span className="flex-1">{error}</span>
            <button
              type="button"
              onClick={() => void doSend()}
              disabled={sending}
              className="rounded-full bg-white/10 px-2.5 py-0.5 text-[11px] font-medium text-white/85 transition hover:bg-white/20 disabled:opacity-40"
            >
              Retry
            </button>
          </div>
        )}

        {route && (
          <div className="flex items-center gap-1.5 text-[11.5px] text-white/65">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-ember-glow"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
            Routed to{" "}
            <span className="font-semibold text-ember-glow">
              {agentRoleLabel(normalizeAgentRole(route.agent))}
            </span>
            {route.aspect ? <span className="text-white/45"> · {route.aspect}</span> : null}
          </div>
        )}

        {/* Teach-time confirmation: the cross-session style this note nudged (§8.6). */}
        {route?.learned && route.learned.length > 0 && (
          <div className="flex items-start gap-1.5 text-[11.5px] text-white/55">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 shrink-0 text-emerald-400"><path d="M20 6 9 17l-5-5" /></svg>
            <span className="leading-snug">
              Learned your taste — <span className="text-white/80">{route.learned.map((l) => l.label).join(" · ")}</span>
            </span>
          </div>
        )}

        {/* Prior directions for the shot on screen (§5.4). */}
        {recent.length > 0 && (
          <div className="mt-0.5">
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-white/30">
              Earlier directions
            </div>
            <ul className="flex max-h-16 flex-col gap-1 overflow-y-auto text-[11px] [scrollbar-width:thin]">
              {recent.slice(0, 4).map((d, i) => (
                <li key={`${d.at}-${i}`} className="flex items-baseline gap-1.5 text-white/55">
                  <span className="shrink-0 text-ember-glow/80">{agentRoleLabel(normalizeAgentRole(d.agent))}</span>
                  <span className="truncate">“{d.note}”</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </form>

      {/* Live crew feed */}
      <div className="border-t border-white/8 px-4 pb-3 pt-2">
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-white/35">Crew</span>
          {budgetRemaining !== null && (
            <span className="rounded-full bg-amber-400/15 px-2 py-0.5 text-[10px] font-medium text-amber-300">
              {Math.round(budgetRemaining)}s of film left
            </span>
          )}
        </div>
        {activity.length > 0 ? (
          <ul className="flex max-h-24 flex-col gap-1.5 overflow-y-auto text-[11.5px] [scrollbar-width:thin]">
            {activity.slice(0, 8).map((entry) => (
              <FeedLine key={entry.id} entry={entry} />
            ))}
          </ul>
        ) : (
          <p className="text-[11.5px] text-white/35">The crew is standing by.</p>
        )}
      </div>
    </div>
  );
}
