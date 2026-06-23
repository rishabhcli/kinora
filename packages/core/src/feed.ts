/**
 * The live agent-activity feed model (§5.4 stretch / §5.5 `AgentActivityFeed`).
 *
 * Both shells stream the §5.6 session events; this module turns each raw
 * {@link KinoraEvent} into a typed, timestamped {@link SessionActivity} the feed
 * UI can render — the crew planning, rendering, QA-ing, and arbitrating in real
 * time. Framework-agnostic and pure, so desktop and mobile share one shape (and
 * it is exhaustively unit-testable).
 */
import type { ConflictDecision, ConflictOption, KinoraEvent } from "./events";

// --- Agent roles (the six-member crew, §5.5 / §6) --------------------------- #

/** The canonical crew role an `agent_activity.agent` string maps to. */
export type AgentRole =
  | "showrunner"
  | "adapter"
  | "continuity"
  | "cinematographer"
  | "generator"
  | "critic"
  | "unknown";

const ROLE_LABELS: Record<AgentRole, string> = {
  showrunner: "Showrunner",
  adapter: "Adapter",
  continuity: "Continuity",
  cinematographer: "Cinematographer",
  generator: "Generator",
  critic: "Critic",
  unknown: "Crew",
};

/**
 * Map a backend agent string to a crew role. The backend uses snake-cased names
 * (`continuity_supervisor`, `cinematographer`, …) and occasionally a display
 * form (`Continuity`); we match defensively on substrings so a rename on either
 * side degrades to `unknown` rather than crashing the feed.
 */
export function normalizeAgentRole(raw: string | null | undefined): AgentRole {
  const s = (raw ?? "").toLowerCase();
  if (!s) return "unknown";
  if (s.includes("showrunner") || s.includes("orchestrat")) return "showrunner";
  if (s.includes("adapter") || s.includes("screenwriter")) return "adapter";
  if (s.includes("continuity")) return "continuity";
  if (s.includes("cinematograph")) return "cinematographer";
  if (s.includes("generator")) return "generator";
  if (s.includes("critic") || s === "qa") return "critic";
  return "unknown";
}

/** Human label for a role, e.g. `continuity` → "Continuity". */
export function agentRoleLabel(role: AgentRole): string {
  return ROLE_LABELS[role];
}

// --- The activity union ----------------------------------------------------- #

export interface BaseActivity {
  /** Monotonic per-session id (newest = highest), used as a stable React key. */
  id: number;
  /** Wall-clock time the event was received, ms since epoch. */
  at: number;
}

/** An agent spoke (plan / decision / routed comment / raised conflict). */
export interface AgentActivity extends BaseActivity {
  kind: "agent";
  role: AgentRole;
  /** The raw backend agent string, kept for fidelity. */
  agent: string;
  message: string;
  /** Optional facet the message concerns ("look", "pacing", "room", …). */
  aspect: string | null;
  shotId: string | null;
  /** True when this message concerns a continuity conflict (§7.2). */
  conflict: boolean;
  /** The conflict this activity concerns, when structured (lets the dispute
   *  dialog stream the Showrunner's arbitration into the right prompt). */
  conflictId: string | null;
  /** The structured decision payload (a director pick or auto-arbitration),
   *  when this activity records a §7.2 resolution rather than a plain message. */
  decision: ConflictDecision | null;
  /** Structured Critic QA (ccs/verdict) when this is a QA result (§9.5). */
  qa: Record<string, unknown> | null;
}

/** A shot was re-rendered after a Director edit (`regen_done`). */
export interface RegenActivity extends BaseActivity {
  kind: "regen";
  shotId: string;
  /** The clip URL the shot had *before* the regen, for a before/after compare. */
  beforeUrl: string | null;
  /** The freshly rendered clip URL. */
  afterUrl: string | null;
  qa: Record<string, unknown> | null;
}

/** The budget dropped below the live-render threshold (`budget_low`). */
export interface BudgetActivity extends BaseActivity {
  kind: "budget";
  remainingS: number;
}

/** A continuity conflict needs the Director to arbitrate (`conflict_choice`, §7.2). */
export interface ConflictActivity extends BaseActivity {
  kind: "conflict";
  conflictId: string;
  /** The fixed honor/surface/evolve options (§7.2). */
  options: ConflictOption[];
  claim: string | null;
  canonFact: string | null;
  shotId: string | null;
  /** The agent that raised it (e.g. `continuity_supervisor`). */
  raisedBy: string | null;
  /** The story beat the violation sits at (e.g. `beat_0039`). */
  currentBeat: string | null;
}

/** A scene was stitched from its per-shot clips (`scene_stitched`). */
export interface SceneActivity extends BaseActivity {
  kind: "scene";
  sceneId: string;
}

export type SessionActivity =
  | AgentActivity
  | RegenActivity
  | BudgetActivity
  | ConflictActivity
  | SceneActivity;

export type ActivityKind = SessionActivity["kind"];

/** The feed retains at most this many entries (oldest dropped). */
export const MAX_SESSION_ACTIVITY = 50;

// --- Event → activity ------------------------------------------------------- #

export interface ActivityContext {
  id: number;
  at: number;
  /** For `regen_done`: the shot's clip URL before the swap (the "before" frame). */
  previousClipUrl?: string | null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * Project a §5.6 event into a feed entry, or `null` for events that don't
 * surface (clip/keyframe hot-swaps, ingest progress). Pure — the caller owns id
 * allocation and the `previousClipUrl` lookup (runtime state the event lacks).
 */
export function activityFromEvent(
  event: KinoraEvent,
  ctx: ActivityContext,
): SessionActivity | null {
  const base = { id: ctx.id, at: ctx.at };
  switch (event.event) {
    case "agent_activity":
      return {
        ...base,
        kind: "agent",
        role: normalizeAgentRole(event.agent),
        agent: event.agent,
        message: event.message,
        aspect: event.aspect ?? null,
        shotId: event.shot_id ?? null,
        // The worker emits a paired agent_activity alongside conflict_choice, and
        // the Showrunner's arbitration reasoning arrives as agent_activity too; we
        // flag/correlate it so the feed can tint it and the dispute dialog can
        // stream the reasoning into the right prompt. The actionable entry is the
        // conflict_choice (which carries the options).
        conflict: event.conflict != null || /conflict/i.test(event.message),
        conflictId: event.conflict?.conflict_id ?? null,
        decision: event.conflict ?? null,
        qa: event.qa ?? null,
      };
    case "regen_done":
      return {
        ...base,
        kind: "regen",
        shotId: event.shot_id,
        beforeUrl: ctx.previousClipUrl ?? null,
        afterUrl: event.oss_url ?? null,
        qa: event.qa ?? null,
      };
    case "budget_low":
      return { ...base, kind: "budget", remainingS: event.remaining_s };
    case "conflict_choice":
      return {
        ...base,
        kind: "conflict",
        conflictId: event.conflict_id,
        options: event.options,
        claim: asString(event.claim),
        canonFact: asString(event.canon_fact),
        shotId: event.shot_id ?? null,
        raisedBy: asString(event.raised_by),
        currentBeat: asString(event.current_beat),
      };
    case "scene_stitched":
      return { ...base, kind: "scene", sceneId: event.scene_id };
    default:
      return null;
  }
}

// --- QA summary (the §5.4 shot badge) --------------------------------------- #

export interface QaSummary {
  /** Character-consistency score, 0..1, or null when absent / not applicable. */
  ccs: number | null;
  /** Composite QA score, 0..1, or null. */
  score: number | null;
  /** Verdict — true = pass, false = fail, null = unknown. */
  passed: boolean | null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Distil a Critic QA record (`{ ccs, score, verdict, … }`, §9.5) into the
 * pass/fail + CCS the badge shows. Defensive: tolerates the verdict arriving as
 * the `Verdict` enum string ("pass"/"fail") or a boolean.
 */
export function summarizeQa(qa: Record<string, unknown> | null | undefined): QaSummary | null {
  if (!qa || typeof qa !== "object") return null;
  const verdict = qa["verdict"];
  let passed: boolean | null = null;
  if (typeof verdict === "boolean") passed = verdict;
  else if (typeof verdict === "string") passed = verdict.toLowerCase() === "pass";
  else if (typeof qa["passed"] === "boolean") passed = qa["passed"] as boolean;
  return { ccs: asNumber(qa["ccs"]), score: asNumber(qa["score"]), passed };
}

/** A compact, human-readable shot id (`shot_ab12cd34…` → "ab12cd34"). */
export function shortShotId(shotId: string): string {
  const tail = shotId.replace(/^shot[_-]?/i, "");
  return tail.length > 8 ? tail.slice(0, 8) : tail;
}

// --- Conflict dispute selectors (§7.2) -------------------------------------- #

/** The option a decision settled on (a director's `option`, else the
 *  Showrunner's auto-arbitrated `chosen_option`), or null for a plain message. */
export function decisionOption(decision: ConflictDecision | null | undefined): string | null {
  if (!decision) return null;
  return decision.chosen_option ?? decision.option ?? null;
}

/** A short label for a §7.2 policy option (`honor_canon` → "honour canon"). */
export function conflictOptionLabel(option: string | null | undefined): string {
  switch (option) {
    case "honor_canon":
      return "honour canon";
    case "evolve_canon":
      return "evolve canon";
    case "surface_to_user":
      return "ask the director";
    default:
      return option ?? "resolve";
  }
}

/** Whether a conflict's loop has closed (its disputed shot re-rendered after the
 *  raise). The director-chosen honor/evolve paths both end in a `regen_done`. */
export function isConflictResolved(
  activity: readonly SessionActivity[],
  conflict: ConflictActivity,
): boolean {
  return activity.some(
    (a) => a.kind === "regen" && a.shotId === conflict.shotId && a.id > conflict.id,
  );
}

/**
 * The newest surfaced conflict still awaiting (or mid-) resolution — what the
 * Crew-dispute dialog should show. Resolved conflicts and any the caller has
 * dismissed are skipped. Pure over the feed, so it is the single source of truth.
 */
export function selectActiveConflict(
  activity: readonly SessionActivity[],
  dismissed?: ReadonlySet<string>,
): ConflictActivity | null {
  for (const a of activity) {
    if (a.kind !== "conflict") continue;
    if (dismissed?.has(a.conflictId)) continue;
    if (isConflictResolved(activity, a)) continue;
    return a;
  }
  return null;
}

/** The streamed resolution of one conflict: the Showrunner's arbitration
 *  reasoning (chronological), the settled option, and whether the loop closed. */
export interface ConflictTrace {
  chosen: string | null;
  reasoning: string[];
  resolved: boolean;
}

/**
 * Distil the resolution trace for `conflict` from the feed: the Showrunner
 * arbitration lines that arrived after it (correlated by `conflictId`), the
 * option they settled on, and whether the disputed shot has re-rendered. Drives
 * the dialog's "arbitrating → resolved" flow.
 */
export function conflictResolution(
  activity: readonly SessionActivity[],
  conflict: ConflictActivity | null,
): ConflictTrace {
  if (!conflict) return { chosen: null, reasoning: [], resolved: false };
  // Oldest → newest so the reasoning reads in order.
  const ordered = [...activity].sort((a, b) => a.id - b.id);
  const reasoning: string[] = [];
  let chosen: string | null = null;
  for (const a of ordered) {
    if (a.id <= conflict.id || a.kind !== "agent") continue;
    if (a.conflictId && a.conflictId !== conflict.conflictId) continue;
    const opt = decisionOption(a.decision);
    if (a.decision || a.conflictId === conflict.conflictId) {
      reasoning.push(a.decision?.reasoning ?? a.message);
      if (opt && !chosen) chosen = opt;
    }
  }
  return { chosen, reasoning, resolved: isConflictResolved(activity, conflict) };
}

// --- One-line summary (the §5.4 status strip) ------------------------------- #

/** A single human-readable line for an activity — the compact strip both shells
 *  show when there isn't room for the full feed. */
export function activitySummary(a: SessionActivity): string {
  switch (a.kind) {
    case "agent":
      return `${agentRoleLabel(a.role)}: ${a.message}`;
    case "regen":
      return `Shot ${shortShotId(a.shotId)} regenerated`;
    case "budget":
      return `Budget low — ${Math.round(a.remainingS)}s of video left`;
    case "conflict":
      return a.claim ? `Continuity dispute — ${a.claim}` : "Continuity raised a dispute";
    case "scene":
      return `Scene ${a.sceneId} stitched`;
    default:
      return "";
  }
}

// --- Grouping (collapse a shot's render lifecycle) -------------------------- #

/** The shot a lifecycle entry belongs to (compose/render/QA + regen), or null.
 *  Continuity-conflict agent lines are excluded so a dispute stays prominent. */
function lifecycleShotOf(a: SessionActivity): string | null {
  if (a.kind === "regen") return a.shotId;
  if (a.kind === "agent" && !a.conflict && a.shotId) return a.shotId;
  return null;
}

/** A run of consecutive lifecycle entries for one shot, newest-first within. */
export interface ShotGroup {
  type: "shot";
  /** Stable key = the newest activity id in the run. */
  id: number;
  shotId: string;
  activities: SessionActivity[];
  latestAt: number;
}

/** A single, ungrouped activity. */
export interface SingleItem {
  type: "single";
  id: number;
  activity: SessionActivity;
}

export type FeedItem = ShotGroup | SingleItem;

/**
 * Collapse consecutive same-shot lifecycle entries (Cinematographer compose →
 * Generator render → Critic QA, plus any regen) into one {@link ShotGroup}, so a
 * burst of per-shot crew chatter reads as a single timeline beat. Conflicts,
 * budget, and scene entries are never grouped (they want individual attention).
 * Pure over the newest-first feed; order is preserved.
 */
export function groupActivity(activity: readonly SessionActivity[]): FeedItem[] {
  const items: FeedItem[] = [];
  let i = 0;
  while (i < activity.length) {
    const a = activity[i]!;
    const shot = lifecycleShotOf(a);
    if (shot === null) {
      items.push({ type: "single", id: a.id, activity: a });
      i += 1;
      continue;
    }
    const run: SessionActivity[] = [];
    let j = i;
    while (j < activity.length && lifecycleShotOf(activity[j]!) === shot) {
      run.push(activity[j]!);
      j += 1;
    }
    if (run.length === 1) {
      items.push({ type: "single", id: a.id, activity: a });
    } else {
      items.push({ type: "shot", id: run[0]!.id, shotId: shot, activities: run, latestAt: run[0]!.at });
    }
    i = j;
  }
  return items;
}

// --- Summary rollup (the §13 efficiency strip) ----------------------------- #

export interface FeedSummary {
  events: number;
  /** Shots the Generator produced (live generator lines + director regens). */
  renders: number;
  /** Critic QA checks observed. */
  qaChecks: number;
  qaPass: number;
  qaFail: number;
  /** Mean character-consistency score across structured QA, 0..1, or null. */
  avgCcs: number | null;
  conflictsRaised: number;
  conflictsResolved: number;
  budgetWarnings: number;
  scenesStitched: number;
}

/**
 * Roll the feed into the headline numbers a judge cares about (§13): how much the
 * crew produced, how QA went, and how many disputes it resolved. Pure + cheap
 * (one pass; the conflict-resolved check is O(n) per conflict but conflicts are
 * rare). CCS is averaged only over entries that carry a structured score.
 */
export function summarizeFeed(activity: readonly SessionActivity[]): FeedSummary {
  let renders = 0;
  let qaChecks = 0;
  let qaPass = 0;
  let qaFail = 0;
  let conflictsRaised = 0;
  let conflictsResolved = 0;
  let budgetWarnings = 0;
  let scenesStitched = 0;
  let ccsSum = 0;
  let ccsN = 0;

  const tallyQa = (qa: Record<string, unknown> | null): void => {
    const s = summarizeQa(qa);
    if (!s) return;
    if (s.passed === true) qaPass += 1;
    else if (s.passed === false) qaFail += 1;
    if (s.ccs !== null) {
      ccsSum += s.ccs;
      ccsN += 1;
    }
  };

  for (const a of activity) {
    switch (a.kind) {
      case "agent":
        if (a.role === "generator") renders += 1;
        if (a.role === "critic") {
          qaChecks += 1;
          tallyQa(a.qa);
        }
        break;
      case "regen":
        renders += 1;
        tallyQa(a.qa);
        break;
      case "conflict":
        conflictsRaised += 1;
        if (isConflictResolved(activity, a)) conflictsResolved += 1;
        break;
      case "budget":
        budgetWarnings += 1;
        break;
      case "scene":
        scenesStitched += 1;
        break;
      default:
        break;
    }
  }

  return {
    events: activity.length,
    renders,
    qaChecks,
    qaPass,
    qaFail,
    avgCcs: ccsN > 0 ? ccsSum / ccsN : null,
    conflictsRaised,
    conflictsResolved,
    budgetWarnings,
    scenesStitched,
  };
}

/** The most-recent agent activity (newest-first feed), for the "now working"
 *  strip — or null when the crew hasn't spoken yet. */
export function latestAgent(activity: readonly SessionActivity[]): AgentActivity | null {
  for (const a of activity) if (a.kind === "agent") return a;
  return null;
}

// --- Export (the §5.4 "without backend logs" log) -------------------------- #

/** Render the feed as a plain-text / Markdown transcript (oldest → newest) the
 *  reader can copy out — the crew's story without opening backend logs. */
export function formatActivityLog(activity: readonly SessionActivity[]): string {
  const ordered = [...activity].sort((a, b) => a.id - b.id);
  const lines = ordered.map((a) => {
    const t = new Date(a.at).toISOString().slice(11, 19);
    return `- ${t}  ${activitySummary(a)}`;
  });
  return `# Kinora — crew activity\n\n${lines.join("\n")}\n`;
}
