/**
 * The §13 crew-vs-baseline eval report and the pure, framework-agnostic helpers
 * both shells render it through — the single source of truth shared by the
 * desktop and mobile metrics surfaces (and unit-tested in isolation).
 *
 * `GET /api/eval/report/{book_id}` is typed on the backend as `dict[str, Any]`
 * (it serves a cached `EvalReport.to_contract()`), so it arrives over the typed
 * client as an opaque record — we pin the exact shape here and narrow into it.
 * Everything below is deterministic so the demo numbers are reproducible: which
 * arm wins each metric, the relative improvement, the shared bar domain, the
 * committed-buffer health (a mirror of the backend's `buffer_health`), the
 * headline verdict, and the copy/markdown exports.
 */

/** One metric's mean for each arm: the crew + shared canon vs the single agent. */
export interface ArmPair {
  crew: number;
  baseline: number;
}

/** The pre-registered thresholds (§9.5 + §13), frozen before any run. */
export interface EvalThresholds {
  ccs_min: number;
  style_drift_max: number;
  motion_artifact_max: number;
  regen_rate_target: number;
  buffer_above_low_target: number;
  stalls_target: number;
}

/** The full cached report — means + spread + per-character CCS + thresholds. */
export interface EvalReport {
  ccs: ArmPair;
  efficiency: ArmPair;
  regen_rate: ArmPair;
  style_drift: ArmPair;
  runs: number;
  thresholds: EvalThresholds;
  per_character_ccs: {
    crew: Record<string, number>;
    baseline: Record<string, number>;
  };
  spread: {
    ccs: ArmPair;
    efficiency: ArmPair;
    regen_rate: ArmPair;
    style_drift: ArmPair;
  };
}

/** The stable `{error: {type, message}}` envelope the gateway returns (§12). */
export interface ErrorEnvelope {
  error: { type: string; message: string; detail?: unknown };
}

/** The four headline metrics, in the order they appear in §13. */
export type MetricKey = "ccs" | "efficiency" | "regen_rate" | "style_drift";

export interface MetricMeta {
  key: MetricKey;
  label: string;
  /** A short label for compact surfaces (chips, the summary block). */
  short: string;
  /** Higher is better (CCS, efficiency) vs lower is better (regen, drift). */
  higherIsBetter: boolean;
  /** A one-line "what this measures", straight from §13. */
  caption: string;
  /** Render a raw value for display (units folded in). */
  format: (v: number) => string;
}

export const METRICS: readonly MetricMeta[] = [
  {
    key: "ccs",
    label: "Character consistency",
    short: "CCS",
    higherIsBetter: true,
    caption: "Mean appearance-embedding cosine vs the locked reference",
    format: (v) => v.toFixed(3),
  },
  {
    key: "efficiency",
    label: "Accepted-footage efficiency",
    short: "Efficiency",
    higherIsBetter: true,
    caption: "QA-passed seconds per 100s of generation budget",
    format: (v) => `${v.toFixed(1)}%`,
  },
  {
    key: "regen_rate",
    label: "Regeneration rate",
    short: "Regen rate",
    higherIsBetter: false,
    caption: "Fraction of shots that failed QA and were re-rendered",
    format: (v) => v.toFixed(2),
  },
  {
    key: "style_drift",
    label: "Style drift",
    short: "Style drift",
    higherIsBetter: false,
    caption: "Variance of style embeddings across a scene",
    format: (v) => v.toFixed(3),
  },
];

/** A light runtime guard so the opaque report record can be narrowed safely. */
export function isEvalReport(value: unknown): value is EvalReport {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.ccs === "object" &&
    typeof v.efficiency === "object" &&
    typeof v.regen_rate === "object" &&
    typeof v.style_drift === "object" &&
    typeof v.runs === "number" &&
    typeof v.thresholds === "object"
  );
}

/** Does the crew beat (or match) the baseline on this metric? A tie is a crew
 *  "win" — it still proves no regression once memory + crew are added. */
export function crewWins(meta: MetricMeta, pair: ArmPair): boolean {
  return meta.higherIsBetter ? pair.crew >= pair.baseline : pair.crew <= pair.baseline;
}

/** Relative improvement of the crew over the baseline as a signed %, where
 *  positive always means "crew is better". `null` when the baseline is 0. */
export function improvementPct(meta: MetricMeta, pair: ArmPair): number | null {
  if (pair.baseline === 0) return null;
  const raw = meta.higherIsBetter
    ? (pair.crew - pair.baseline) / Math.abs(pair.baseline)
    : (pair.baseline - pair.crew) / Math.abs(pair.baseline);
  return raw * 100;
}

/** The bar-chart domain max so both arms share one scale. CCS is a cosine in
 *  [0,1]; efficiency a percentage in [0,100]; the variance metrics have no fixed
 *  ceiling, so they're framed against the larger arm (with headroom). */
export function metricDomainMax(meta: MetricMeta, pair: ArmPair): number {
  if (meta.key === "ccs") return 1;
  if (meta.key === "efficiency") return 100;
  const peak = Math.max(pair.crew, pair.baseline);
  return peak > 0 ? peak * 1.25 : 1;
}

/** A value's fraction of the domain, clamped to [0, 1] (a bar width). */
export function barFraction(value: number, domainMax: number): number {
  if (domainMax <= 0) return 0;
  return Math.max(0, Math.min(1, value / domainMax));
}

/** The pre-registered gate for a metric, where one applies (efficiency has none). */
export function metricThreshold(meta: MetricMeta, t: EvalThresholds): number | null {
  switch (meta.key) {
    case "ccs":
      return t.ccs_min;
    case "regen_rate":
      return t.regen_rate_target;
    case "style_drift":
      return t.style_drift_max;
    default:
      return null;
  }
}

/** Whether a value meets its pre-registered gate (`null` when none applies). */
export function meetsThreshold(
  meta: MetricMeta,
  value: number,
  t: EvalThresholds,
): boolean | null {
  const th = metricThreshold(meta, t);
  if (th === null) return null;
  return meta.higherIsBetter ? value >= th : value <= th;
}

// --------------------------------------------------------------------------- //
// Headline verdict — the single most persuasive line for a judge
// --------------------------------------------------------------------------- //

export interface ReportVerdict {
  /** Metrics where the crew beats (or matches) the baseline. */
  wins: number;
  /** Total headline metrics (always `METRICS.length`). */
  total: number;
  /** Pre-registered gates the crew clears. */
  gatesMet: number;
  /** Pre-registered gates that apply (efficiency has none). */
  gatesTotal: number;
  /** Whether the crew clears the identity (CCS) gate specifically. */
  ccsGateMet: boolean;
  /** Whether the crew wins (or ties) every headline metric. */
  sweep: boolean;
  /** A one-line summary, e.g. "Crew beats baseline on 4/4 · 3/3 gates met". */
  headline: string;
}

/** Fold the report into a one-line verdict (pure → shared + testable). */
export function reportVerdict(report: EvalReport): ReportVerdict {
  let wins = 0;
  let gatesMet = 0;
  let gatesTotal = 0;
  for (const meta of METRICS) {
    const pair = report[meta.key];
    if (crewWins(meta, pair)) wins += 1;
    const gate = meetsThreshold(meta, pair.crew, report.thresholds);
    if (gate !== null) {
      gatesTotal += 1;
      if (gate) gatesMet += 1;
    }
  }
  const total = METRICS.length;
  const ccsGateMet = report.ccs.crew >= report.thresholds.ccs_min;
  const sweep = wins === total;
  const headline =
    `Crew beats the single-agent baseline on ${wins}/${total} metrics` +
    (gatesTotal > 0 ? ` · ${gatesMet}/${gatesTotal} pre-registered gates met` : "");
  return { wins, total, gatesMet, gatesTotal, ccsGateMet, sweep, headline };
}

// --------------------------------------------------------------------------- //
// Committed-buffer health — a mirror of the backend §13 `buffer_health`
// --------------------------------------------------------------------------- //

/** One sample on the committed-buffer sawtooth (the shared API contract item). */
export interface BufferPoint {
  t: number;
  committed_seconds_ahead: number;
  low: number;
  high: number;
}

export interface BufferHealthSummary {
  /** Time-weighted fraction of reading-time the buffer stayed at/above `L`. */
  fractionAboveLow: number;
  /** Visible stalls — onsets of an empty buffer (`committed <= 0`). */
  stalls: number;
  /** Total reading-time spanned by the trace, in seconds. */
  durationS: number;
}

/** Reading-time span of a trace (last − first sample), or 0 if degenerate. */
function bufferSpan(trace: readonly BufferPoint[]): number {
  const first = trace.at(0);
  const last = trace.at(-1);
  if (!first || !last) return 0;
  return Math.max(0, last.t - first.t);
}

/**
 * Mirror of `app.eval.metrics.buffer_health`: the time-weighted fraction of
 * reading-time the committed buffer stayed at/above its own low watermark, plus
 * the stall count. Each sample holds until the next (a step function), so uneven
 * tick spacing is handled correctly.
 */
export function bufferHealth(trace: readonly BufferPoint[]): BufferHealthSummary {
  const n = trace.length;
  if (n === 0) return { fractionAboveLow: 1, stalls: 0, durationS: 0 };

  let stalls = 0;
  let inStall = false;
  let aboveTime = 0;
  let totalTime = 0;
  // Each sample's dt (= gap to the next) is weighted by whether *it* was above
  // `L`; walking with `prev` attributes that interval to the earlier sample, so
  // the final sample contributes dt 0 — exactly the backend's step function.
  let prev: BufferPoint | undefined;
  for (const s of trace) {
    const stalledNow = s.committed_seconds_ahead <= 0;
    if (stalledNow && !inStall) stalls += 1;
    inStall = stalledNow;
    if (prev) {
      const dt = Math.max(0, s.t - prev.t);
      totalTime += dt;
      if (prev.committed_seconds_ahead >= prev.low) aboveTime += dt;
    }
    prev = s;
  }

  if (totalTime <= 0) {
    // Degenerate (single sample / zero-length): fall back to a count fraction.
    const aboveCount = trace.filter((s) => s.committed_seconds_ahead >= s.low).length;
    return { fractionAboveLow: aboveCount / n, stalls, durationS: bufferSpan(trace) };
  }
  return { fractionAboveLow: aboveTime / totalTime, stalls, durationS: totalTime };
}

// --------------------------------------------------------------------------- //
// Copy-friendly exports
// --------------------------------------------------------------------------- //

function signedPct(value: number | null): string {
  if (value === null) return "n/a";
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;
}

/** A plain-text, paste-ready summary of the proof for a demo slide. */
export function summarizeReport(
  report: EvalReport,
  health: BufferHealthSummary | null,
): string {
  const lines: string[] = [];
  const runs = `${report.runs} run${report.runs === 1 ? "" : "s"}`;
  lines.push(`Kinora §13 — crew + shared canon vs single-agent baseline (${runs})`);
  lines.push("same book, same seeds, same prompts; only memory + crew differ.");
  lines.push("");

  for (const meta of METRICS) {
    const pair = report[meta.key];
    const label = `${meta.label}:`.padEnd(30, " ");
    const crew = `crew ${meta.format(pair.crew)}`.padEnd(15, " ");
    const base = `vs baseline ${meta.format(pair.baseline)}`.padEnd(22, " ");
    const gate = meetsThreshold(meta, pair.crew, report.thresholds);
    const gateNote = gate === null ? "" : gate ? "  [gate met]" : "  [gate missed]";
    lines.push(`  ${label}${crew}${base}${signedPct(improvementPct(meta, pair))}${gateNote}`);
  }

  if (health) {
    lines.push("");
    const above = (health.fractionAboveLow * 100).toFixed(1);
    const stalls = `${health.stalls} stall${health.stalls === 1 ? "" : "s"}`;
    lines.push(`  Committed buffer: ${above}% of reading-time above L, ${stalls}.`);
  }
  lines.push("");
  lines.push(`  ${reportVerdict(report).headline}.`);
  return lines.join("\n");
}

/** A GitHub-flavoured Markdown table of the proof — for the README / slides. */
export function reportToMarkdown(
  report: EvalReport,
  health: BufferHealthSummary | null,
): string {
  const verdict = reportVerdict(report);
  const lines: string[] = [];
  lines.push(
    `### Kinora §13 — crew vs single-agent baseline (${report.runs} run${report.runs === 1 ? "" : "s"})`,
  );
  lines.push("");
  lines.push("| Metric | Crew | Baseline | Δ | Gate |");
  lines.push("| --- | ---: | ---: | ---: | :---: |");
  for (const meta of METRICS) {
    const pair = report[meta.key];
    const gate = meetsThreshold(meta, pair.crew, report.thresholds);
    const gateCell = gate === null ? "—" : gate ? "✓" : "✗";
    const better = meta.higherIsBetter ? "↑" : "↓";
    lines.push(
      `| ${meta.label} ${better} | ${meta.format(pair.crew)} | ${meta.format(pair.baseline)} | ${signedPct(improvementPct(meta, pair))} | ${gateCell} |`,
    );
  }
  if (health) {
    lines.push("");
    lines.push(
      `**Committed buffer:** ${(health.fractionAboveLow * 100).toFixed(1)}% of reading-time above L, ${health.stalls} stall${health.stalls === 1 ? "" : "s"}.`,
    );
  }
  lines.push("");
  lines.push(`_${verdict.headline}._`);
  return lines.join("\n");
}
