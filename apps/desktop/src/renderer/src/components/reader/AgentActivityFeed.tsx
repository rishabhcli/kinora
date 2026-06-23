import {
  type ActivityKind,
  type AgentActivity,
  type AgentRole,
  agentRoleLabel,
  activitySummary,
  type ConflictActivity,
  type FeedSummary,
  formatActivityLog,
  groupActivity,
  latestAgent,
  type RegenActivity,
  type SessionActivity,
  type ShotGroup,
  type SocketStatus,
  shortShotId,
  summarizeFeed,
  summarizeQa,
} from "@kinora/core";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

interface AgentActivityFeedProps {
  /** The §5.4 feed, newest first. */
  activity: SessionActivity[];
  socketStatus: SocketStatus;
  open: boolean;
  onToggle: () => void;
  /** Jump the reading + playhead to a shot (regen entry → timeline). */
  onSelectShot?: (shotId: string) => void;
  /** Open the conflict-resolution modal for a surfaced conflict (§7.2). */
  onResolveConflict?: (conflict: ConflictActivity) => void;
}

type FilterKind = ActivityKind | "all";

const FILTERS: { id: FilterKind; label: string }[] = [
  { id: "all", label: "All" },
  { id: "agent", label: "Crew" },
  { id: "regen", label: "Renders" },
  { id: "conflict", label: "Conflicts" },
  { id: "scene", label: "Scenes" },
  { id: "budget", label: "Budget" },
];

/** Per-role accent + glyph for the agent avatar (the six-member crew, §5.5). */
const ROLE_META: Record<AgentRole, { tint: string; icon: ReactNode }> = {
  showrunner: {
    tint: "bg-ember/20 text-ember-glow ring-ember-glow/30",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 5h16v14H4z" /><path d="M4 9h16M9 5 7 9M15 5l-2 4" />
      </svg>
    ),
  },
  adapter: {
    tint: "bg-violet-400/15 text-violet-200 ring-violet-300/30",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
      </svg>
    ),
  },
  continuity: {
    tint: "bg-emerald-400/15 text-emerald-200 ring-emerald-300/30",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" /><path d="m9 12 2 2 4-4" />
      </svg>
    ),
  },
  cinematographer: {
    tint: "bg-sky-400/15 text-sky-200 ring-sky-300/30",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 7h13v10H2zM15 10l5-3v10l-5-3" /><circle cx="7" cy="12" r="2.2" />
      </svg>
    ),
  },
  generator: {
    tint: "bg-fuchsia-400/15 text-fuchsia-200 ring-fuchsia-300/30",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="m12 3 1.8 4.2L18 9l-4.2 1.8L12 15l-1.8-4.2L6 9l4.2-1.8Z" /><path d="M18 15v4M16 17h4" />
      </svg>
    ),
  },
  critic: {
    tint: "bg-amber-400/15 text-amber-200 ring-amber-300/30",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M1.5 12S5 5 12 5s10.5 7 10.5 7-3.5 7-10.5 7S1.5 12 1.5 12Z" /><circle cx="12" cy="12" r="2.6" />
      </svg>
    ),
  },
  unknown: {
    tint: "bg-white/10 text-white/70 ring-white/20",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="9" cy="8" r="3.2" /><path d="M3.5 19a5.5 5.5 0 0 1 11 0M16 11l2 2 4-4" />
      </svg>
    ),
  },
};

/** Accent dot per kind — also the filter-chip and non-agent entry color. */
const KIND_DOT: Record<ActivityKind, string> = {
  agent: "bg-ember-glow",
  regen: "bg-sky-400",
  budget: "bg-amber-400",
  conflict: "bg-rose-400",
  scene: "bg-emerald-400",
};

/** Pulse color per role for the "now working" strip on the collapsed toggle. */
const ROLE_PULSE: Record<AgentRole, string> = {
  showrunner: "text-ember-glow",
  adapter: "text-violet-300",
  continuity: "text-emerald-300",
  cinematographer: "text-sky-300",
  generator: "text-fuchsia-300",
  critic: "text-amber-300",
  unknown: "text-white/50",
};

const LINK_META: Record<SocketStatus, { label: string; dot: string; live: boolean }> = {
  open: { label: "Live", dot: "text-emerald-400", live: true },
  connecting: { label: "Reconnecting", dot: "text-amber-400", live: true },
  closed: { label: "Offline", dot: "text-white/35", live: false },
};

function relativeTime(at: number, now: number): string {
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 5) return "now";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h`;
}

/** A live-status dot — pulses while linked, static when offline; reduce-motion safe. */
function LiveDot({ status }: { status: SocketStatus }) {
  const meta = LINK_META[status];
  return <span className={`status-pulse ${meta.dot}`} data-live={meta.live} aria-hidden="true" />;
}

function Timestamp({ at, now }: { at: number; now: number }) {
  return (
    <time
      dateTime={new Date(at).toISOString()}
      title={new Date(at).toLocaleTimeString()}
      className="shrink-0 font-sans text-[10.5px] tabular-nums text-white/35"
    >
      {relativeTime(at, now)}
    </time>
  );
}

/** The §13 efficiency strip — what the crew produced, distilled from the feed. */
function SummaryBar({ summary }: { summary: FeedSummary }) {
  const pass = summary.qaPass + summary.qaFail;
  const rate = pass > 0 ? Math.round((summary.qaPass / pass) * 100) : null;
  const stats: { label: string; value: string }[] = [
    { label: "rendered", value: String(summary.renders) },
  ];
  if (rate !== null) stats.push({ label: "QA pass", value: `${rate}%` });
  if (summary.avgCcs !== null) stats.push({ label: "avg CCS", value: summary.avgCcs.toFixed(2) });
  if (summary.conflictsRaised > 0) {
    stats.push({ label: "conflicts", value: `${summary.conflictsResolved}/${summary.conflictsRaised}` });
  }
  if (summary.scenesStitched > 0) stats.push({ label: "scenes", value: String(summary.scenesStitched) });

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-white/10 px-3.5 py-2">
      {stats.map((s) => (
        <span key={s.label} className="flex items-baseline gap-1">
          <span className="font-sans text-[13px] font-semibold tabular-nums text-parchment">{s.value}</span>
          <span className="text-[10px] uppercase tracking-wide text-white/40">{s.label}</span>
        </span>
      ))}
    </div>
  );
}

function AgentEntry({ item }: { item: AgentActivity }) {
  const meta = ROLE_META[item.role];
  return (
    <div className="flex gap-2.5">
      <span
        className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ring-1 [&>svg]:h-3.5 [&>svg]:w-3.5 ${meta.tint}`}
      >
        {meta.icon}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-[12px] font-semibold text-parchment">{agentRoleLabel(item.role)}</span>
          {item.aspect && (
            <span className="shrink-0 rounded-full bg-white/8 px-1.5 py-px text-[9.5px] font-medium uppercase tracking-wide text-white/55">
              {item.aspect}
            </span>
          )}
        </div>
        <p className="mt-0.5 break-words text-[12px] leading-snug text-white/65">{item.message}</p>
      </div>
    </div>
  );
}

function ClipThumb({ url, label }: { url: string | null; label: string }) {
  return (
    <div className="flex-1">
      <span className="mb-1 block text-[9px] font-medium uppercase tracking-wide text-white/40">{label}</span>
      {url ? (
        <video
          src={url}
          muted
          playsInline
          preload="metadata"
          className="pointer-events-none aspect-video w-full rounded-md bg-black object-cover ring-1 ring-white/10"
        />
      ) : (
        <div className="flex aspect-video w-full items-center justify-center rounded-md bg-white/5 text-[9px] text-white/30 ring-1 ring-white/10">
          —
        </div>
      )}
    </div>
  );
}

function RegenEntry({
  item,
  onSelectShot,
}: {
  item: RegenActivity;
  onSelectShot?: (shotId: string) => void;
}) {
  const qa = summarizeQa(item.qa);
  const hasThumbs = Boolean(item.beforeUrl || item.afterUrl);
  return (
    <button
      type="button"
      onClick={() => onSelectShot?.(item.shotId)}
      disabled={!onSelectShot}
      className="group/regen w-full rounded-lg p-1.5 text-left transition enabled:hover:bg-white/5 disabled:cursor-default focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow/60"
    >
      <div className="flex items-center gap-1.5">
        <span className="text-[12px] font-semibold text-sky-200">Shot re-rendered</span>
        <span className="font-sans text-[10.5px] text-white/40">·</span>
        <span className="truncate font-sans text-[10.5px] tabular-nums text-white/45 underline-offset-2 group-hover/regen:underline">
          {shortShotId(item.shotId)}
        </span>
      </div>
      {qa && (
        <div className="mt-1 flex items-center gap-1.5">
          {qa.passed !== null && (
            <span
              className={`rounded-full px-1.5 py-px text-[9.5px] font-semibold uppercase tracking-wide ${
                qa.passed ? "bg-emerald-400/15 text-emerald-200" : "bg-rose-400/15 text-rose-200"
              }`}
            >
              {qa.passed ? "QA pass" : "QA fail"}
            </span>
          )}
          {qa.ccs !== null && (
            <span className="rounded-full bg-white/8 px-1.5 py-px text-[9.5px] font-medium tabular-nums text-white/60">
              CCS {qa.ccs.toFixed(2)}
            </span>
          )}
        </div>
      )}
      {hasThumbs && (
        <div className="mt-1.5 flex gap-2">
          <ClipThumb url={item.beforeUrl} label="Before" />
          <ClipThumb url={item.afterUrl} label="After" />
        </div>
      )}
    </button>
  );
}

function optionLabel(option: unknown): { id: string | null; label: string } {
  if (typeof option === "string") return { id: option, label: option.replace(/_/g, " ") };
  if (option && typeof option === "object") {
    const o = option as Record<string, unknown>;
    const id = typeof o["id"] === "string" ? (o["id"] as string) : null;
    const action = typeof o["action"] === "string" ? (o["action"] as string) : null;
    return { id, label: action ?? id?.replace(/_/g, " ") ?? "option" };
  }
  return { id: null, label: "option" };
}

function ConflictEntry({
  item,
  onResolveConflict,
}: {
  item: ConflictActivity;
  onResolveConflict?: (conflict: ConflictActivity) => void;
}) {
  return (
    <div className="rounded-lg bg-rose-400/[0.07] p-2 ring-1 ring-rose-400/20">
      <div className="flex items-center gap-1.5">
        <span className="text-[12px] font-semibold text-rose-200">Continuity conflict</span>
        {item.shotId && (
          <span className="truncate font-sans text-[10.5px] tabular-nums text-white/40">{shortShotId(item.shotId)}</span>
        )}
      </div>
      {item.claim && <p className="mt-1 break-words text-[12px] leading-snug text-white/70">{item.claim}</p>}
      {item.canonFact && (
        <p className="mt-1 break-words text-[11px] leading-snug text-white/45">Canon: {item.canonFact}</p>
      )}
      {item.options.length > 0 && (
        <ul className="mt-1.5 flex flex-wrap gap-1">
          {item.options.map((opt, i) => (
            <li key={i} className="rounded-full bg-white/8 px-1.5 py-px text-[9.5px] font-medium capitalize text-white/55">
              {optionLabel(opt).label}
            </li>
          ))}
        </ul>
      )}
      {onResolveConflict && (
        <button
          type="button"
          onClick={() => onResolveConflict(item)}
          className="mt-2 rounded-full bg-rose-400/90 px-2.5 py-1 text-[11px] font-semibold text-walnut-deep transition hover:bg-rose-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-200"
        >
          Resolve…
        </button>
      )}
    </div>
  );
}

function SimpleEntry({ dot, label, detail }: { dot: string; label: string; detail?: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
      <p className="min-w-0 flex-1 break-words text-[12px] leading-snug text-white/70">
        <span className="font-semibold text-parchment">{label}</span>
        {detail && <span className="text-white/55"> — {detail}</span>}
      </p>
    </div>
  );
}

/** Render one activity (used by single rows and inside a shot group). */
function Entry({
  item,
  onSelectShot,
  onResolveConflict,
}: {
  item: SessionActivity;
  onSelectShot?: (shotId: string) => void;
  onResolveConflict?: (conflict: ConflictActivity) => void;
}) {
  switch (item.kind) {
    case "agent":
      return <AgentEntry item={item} />;
    case "regen":
      return <RegenEntry item={item} onSelectShot={onSelectShot} />;
    case "conflict":
      return <ConflictEntry item={item} onResolveConflict={onResolveConflict} />;
    case "budget":
      return <SimpleEntry dot={KIND_DOT.budget} label="Budget low" detail={`${Math.round(item.remainingS)}s of film left`} />;
    case "scene":
      return <SimpleEntry dot={KIND_DOT.scene} label="Scene stitched" detail={shortShotId(item.sceneId)} />;
    default:
      return null;
  }
}

/** A collapsed shot lifecycle: the newest step + a toggle to reveal the rest. */
function ShotGroupRow({
  group,
  now,
  onSelectShot,
}: {
  group: ShotGroup;
  now: number;
  onSelectShot?: (shotId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [head, ...rest] = group.activities;
  if (!head) return null;
  return (
    <div className="rounded-lg ring-1 ring-white/[0.06]">
      <div className="flex items-start gap-2 p-1.5">
        <button
          type="button"
          onClick={() => onSelectShot?.(group.shotId)}
          disabled={!onSelectShot}
          className="min-w-0 flex-1 rounded text-left disabled:cursor-default focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow/60"
          aria-label={`Shot ${shortShotId(group.shotId)} — ${group.activities.length} crew steps`}
        >
          <Entry item={head} onSelectShot={onSelectShot} />
        </button>
        <Timestamp at={head.at} now={now} />
      </div>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-1 px-2.5 pb-1.5 text-[10.5px] font-medium text-white/40 transition hover:text-white/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow/50"
      >
        <svg
          width="11"
          height="11"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`transition-transform motion-reduce:transition-none ${expanded ? "rotate-90" : ""}`}
        >
          <path d="M9 6l6 6-6 6" />
        </svg>
        {expanded ? "Hide steps" : `${rest.length} earlier ${rest.length === 1 ? "step" : "steps"} on shot ${shortShotId(group.shotId)}`}
      </button>
      {expanded && (
        <ul className="space-y-2 border-t border-white/[0.06] px-2.5 py-2">
          {rest.map((a) => (
            <li key={a.id} className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <Entry item={a} onSelectShot={onSelectShot} />
              </div>
              <Timestamp at={a.at} now={now} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The §5.4 live agent-activity feed: a collapsible right-edge drawer that turns
 * the multi-agent negotiation into something a judge can watch — the crew
 * planning (agent), rendering + QA (regen), arbitrating (conflict), stitching
 * (scene), and budget pressure — in real time, without backend logs. It overlays
 * the cinema pane only, so the reading pane never loses focus. Consecutive
 * per-shot crew steps collapse into one group; a summary strip rolls up the §13
 * efficiency numbers; entries are searchable and exportable; reduce-motion safe;
 * and the stream is a polite ARIA live region for screen readers.
 */
export function AgentActivityFeed({
  activity,
  socketStatus,
  open,
  onToggle,
  onSelectShot,
  onResolveConflict,
}: AgentActivityFeedProps) {
  const [filter, setFilter] = useState<FilterKind>("all");
  const [query, setQuery] = useState("");
  const [copied, setCopied] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const searchRef = useRef<HTMLInputElement | null>(null);

  // Tick so relative timestamps + the "now working" strip stay live (a touch
  // faster while open). Cheap: only `now` changes; the heavy rollups are memoized.
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), open ? 5_000 : 3_000);
    return () => clearInterval(t);
  }, [open]);

  // Keyboard: F toggles the feed; / focuses search when open; Esc closes. Ignored
  // while typing in a field so it never fights the reader's text entry.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const el = e.target as HTMLElement | null;
      const typing = !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
      if (e.key === "Escape" && open) {
        onToggle();
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "f" || e.key === "F") {
        e.preventDefault();
        onToggle();
      } else if (e.key === "/" && open) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onToggle]);

  // Unread badge: items newer than the last one seen while the drawer was open.
  const seenRef = useRef(-1);
  const [unread, setUnread] = useState(0);
  useEffect(() => {
    const newest = activity[0]?.id ?? -1;
    if (newest < seenRef.current) seenRef.current = -1; // session reset (ids restart)
    if (open) {
      seenRef.current = newest;
      setUnread(0);
    } else {
      setUnread(activity.filter((a) => a.id > seenRef.current).length);
    }
  }, [activity, open]);

  // Attention toasts: while the drawer is closed, a fresh conflict or budget_low
  // raises a transient notice that opens the feed — surfaced, never focus-stealing.
  const [toast, setToast] = useState<SessionActivity | null>(null);
  const seenAttnRef = useRef(-1);
  useEffect(() => {
    const newest = activity[0]?.id ?? -1;
    if (newest < seenAttnRef.current) seenAttnRef.current = -1;
    if (open) {
      seenAttnRef.current = newest;
      setToast(null);
      return;
    }
    const attn = activity.find(
      (a) => (a.kind === "conflict" || a.kind === "budget") && a.id > seenAttnRef.current,
    );
    if (attn) {
      seenAttnRef.current = attn.id;
      setToast(attn);
    }
  }, [activity, open]);
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 6_000);
    return () => window.clearTimeout(t);
  }, [toast]);

  const link = LINK_META[socketStatus];
  const summary = useMemo(() => summarizeFeed(activity), [activity]);
  // The most-recent crew action, shown live on the collapsed toggle if fresh.
  const recentAgent = latestAgent(activity);
  const working = recentAgent && now - recentAgent.at < 10_000 ? recentAgent : null;

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = filter === "all" ? activity : activity.filter((a) => a.kind === filter);
    if (q) {
      list = list.filter((a) => {
        const hay = `${activitySummary(a)} ${a.kind === "agent" ? a.agent : ""}`.toLowerCase();
        return hay.includes(q);
      });
    }
    return groupActivity(list);
  }, [activity, filter, query]);

  function copyLog(): void {
    void navigator.clipboard
      ?.writeText(formatActivityLog(activity))
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => undefined);
  }

  return (
    <>
      {/* Floating toggle (shown when collapsed) — the always-available way in. */}
      {!open && (
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={false}
          className="glass-strong absolute right-3 top-3 z-20 flex items-center gap-2 rounded-full px-3 py-1.5 text-[12px] font-medium text-white/85 shadow-lg transition hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
        >
          {working ? (
            <span className={`status-pulse ${ROLE_PULSE[working.role]}`} data-live="true" aria-hidden="true" />
          ) : (
            <LiveDot status={socketStatus} />
          )}
          <span className="max-w-[13rem] truncate">
            {working ? `${agentRoleLabel(working.role)} · ${working.message}` : "Crew activity"}
          </span>
          {unread > 0 && (
            <span className="flex h-4 min-w-4 items-center justify-center rounded-full bg-ember px-1 text-[10px] font-bold tabular-nums text-walnut-deep">
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </button>
      )}

      {/* Attention toast (closed feed) — a conflict/budget notice that opens it. */}
      {!open && toast && (
        <div
          role="status"
          className="absolute right-3 top-[3.75rem] z-30 flex w-[clamp(240px,26vw,320px)] items-start gap-2.5 rounded-2xl bg-walnut-deep/95 p-3 shadow-xl ring-1 ring-white/12 [backdrop-filter:blur(12px)] animate-[kinora-fade-in_200ms_ease-out] motion-reduce:animate-none"
        >
          <span
            className={`mt-0.5 h-2 w-2 shrink-0 rounded-full ${toast.kind === "conflict" ? "bg-rose-400" : "bg-amber-400"}`}
          />
          <div className="min-w-0 flex-1">
            <p className="text-[12px] font-semibold text-parchment">
              {toast.kind === "conflict" ? "Continuity needs a decision" : "Budget running low"}
            </p>
            <p className="mt-0.5 truncate text-[11px] text-white/55">{activitySummary(toast)}</p>
            <button
              type="button"
              onClick={onToggle}
              className="mt-1.5 rounded-full bg-white/90 px-2.5 py-0.5 text-[11px] font-semibold text-walnut-deep transition hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
            >
              Open feed
            </button>
          </div>
          <button
            type="button"
            onClick={() => setToast(null)}
            aria-label="Dismiss notice"
            className="shrink-0 text-white/40 transition hover:text-white/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>
      )}

      {/* The drawer — overlays the cinema pane; reading pane is never covered. */}
      <aside
        aria-hidden={!open}
        aria-label="Agent activity feed"
        className={`popover absolute bottom-3 right-3 top-3 z-20 flex w-[clamp(280px,30vw,380px)] flex-col overflow-hidden transition-transform duration-300 ease-out motion-reduce:transition-none ${
          open ? "translate-x-0" : "pointer-events-none translate-x-[calc(100%+1.5rem)]"
        }`}
      >
        {/* Header */}
        <header className="flex items-center gap-2 border-b border-white/10 px-3.5 py-2.5">
          <h2 className="font-display text-[14px] text-parchment">Crew activity</h2>
          <span className="flex items-center gap-1 text-[10.5px] font-medium text-white/45">
            <LiveDot status={socketStatus} />
            {link.label}
          </span>
          <button
            type="button"
            onClick={copyLog}
            aria-label="Copy activity log"
            title="Copy the crew log"
            className="ml-auto flex h-7 items-center gap-1 rounded-full px-2 text-[10.5px] font-medium text-white/55 transition hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
          >
            {copied ? (
              <>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                Copied
              </>
            ) : (
              <>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></svg>
                Copy
              </>
            )}
          </button>
          <button
            type="button"
            onClick={onToggle}
            aria-label="Collapse feed"
            className="flex h-7 w-7 items-center justify-center rounded-full text-white/55 transition hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 6l6 6-6 6" />
            </svg>
          </button>
        </header>

        {summary.events > 0 && <SummaryBar summary={summary} />}

        {/* Filter chips — group the stream by kind without losing chronology. */}
        <div className="flex flex-wrap gap-1 px-3 pt-2">
          {FILTERS.map((f) => {
            const count = f.id === "all" ? activity.length : activity.filter((a) => a.kind === f.id).length;
            const active = filter === f.id;
            return (
              <button
                key={f.id}
                type="button"
                onClick={() => setFilter(f.id)}
                aria-pressed={active}
                className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow ${
                  active ? "bg-white/18 text-parchment" : "text-white/45 hover:bg-white/8 hover:text-white/75"
                }`}
              >
                {f.id !== "all" && <span className={`h-1.5 w-1.5 rounded-full ${KIND_DOT[f.id]}`} />}
                {f.label}
                {count > 0 && <span className="tabular-nums text-white/35">{count}</span>}
              </button>
            );
          })}
        </div>

        {/* Search */}
        <div className="px-3 py-2">
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search the crew log…"
            aria-label="Search activity"
            className="glass-input w-full rounded-full px-3 py-1.5 text-[12px]"
          />
        </div>

        {/* Stream — a polite live region so new crew activity is announced. */}
        <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3" role="log" aria-live="polite" aria-relevant="additions">
          {shown.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-white/5 text-white/40">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M4 5h16v14H4z" /><path d="M4 9h16M9 5 7 9M15 5l-2 4" />
                </svg>
              </span>
              <p className="text-[12px] text-white/45">
                {activity.length === 0
                  ? "The crew is standing by."
                  : query.trim()
                    ? "No entries match your search."
                    : "No entries of this kind yet."}
              </p>
            </div>
          ) : (
            <ul className="flex flex-col gap-2.5">
              {shown.map((item) =>
                item.type === "shot" ? (
                  <li key={item.id} className="feed-item">
                    <ShotGroupRow group={item} now={now} onSelectShot={onSelectShot} />
                  </li>
                ) : (
                  <li key={item.id} className="feed-item flex items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <Entry item={item.activity} onSelectShot={onSelectShot} onResolveConflict={onResolveConflict} />
                    </div>
                    <Timestamp at={item.activity.at} now={now} />
                  </li>
                ),
              )}
            </ul>
          )}
        </div>
      </aside>
    </>
  );
}
