import {
  type ArmPair,
  type EvalReport,
  type MetricMeta,
  METRICS,
  barFraction,
  crewWins,
  improvementPct,
  meetsThreshold,
  metricDomainMax,
  metricThreshold,
} from "@kinora/core";

/** A single arm's bar: a label, a track scaled to the shared metric domain (with
 *  the value pinned to its end), where the winning arm is inked ember. */
function ArmBar({
  arm,
  value,
  std,
  fraction,
  winner,
  format,
}: {
  arm: string;
  value: number;
  std: number;
  fraction: number;
  winner: boolean;
  format: (v: number) => string;
}) {
  return (
    <div className="grid grid-cols-[3.2rem_1fr_auto] items-center gap-2.5">
      <span className={`text-[11px] ${winner ? "text-white/80" : "text-white/45"}`}>{arm}</span>
      <span className="relative h-2.5 overflow-hidden rounded-full bg-white/8">
        <span
          className={`absolute inset-y-0 left-0 rounded-full transition-[width] duration-700 ease-out ${
            winner
              ? "bg-gradient-to-r from-ember-deep to-ember-glow shadow-[0_0_12px_-2px_rgba(244,168,93,0.7)]"
              : "bg-white/25"
          }`}
          style={{ width: `${Math.max(fraction * 100, 1.5)}%` }}
        />
      </span>
      <span
        className={`w-[5.5rem] text-right font-sans text-[12px] tabular-nums ${
          winner ? "font-semibold text-white" : "text-white/55"
        }`}
      >
        {format(value)}
        {std > 0 && <span className="ml-1 text-[10px] text-white/35">±{format(std)}</span>}
      </span>
    </div>
  );
}

/** One metric: the crew vs baseline bars, an improvement badge, the shared
 *  domain, and the pre-registered gate drawn as a tick + a pass/fail note. */
function MetricTile({
  meta,
  pair,
  spread,
  report,
}: {
  meta: MetricMeta;
  pair: ArmPair;
  spread: ArmPair;
  report: EvalReport;
}) {
  const domainMax = metricDomainMax(meta, pair);
  const crewBetter = crewWins(meta, pair);
  const imp = improvementPct(meta, pair);
  const gate = meetsThreshold(meta, pair.crew, report.thresholds);
  const threshold = metricThreshold(meta, report.thresholds);
  const tickFraction = threshold === null ? null : threshold / domainMax;
  const showTick = tickFraction !== null && tickFraction > 0.02 && tickFraction < 0.98;

  return (
    <div className="rounded-2xl border border-white/8 bg-white/[0.03] p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-display text-[14px] font-semibold text-white">{meta.label}</p>
          <p className="mt-0.5 text-[11px] leading-snug text-white/45">{meta.caption}</p>
        </div>
        {imp !== null && (
          <span
            className={`shrink-0 rounded-full px-2 py-1 text-[11px] font-semibold tabular-nums ${
              crewBetter ? "bg-emerald-400/15 text-emerald-300" : "bg-rose-400/15 text-rose-300"
            }`}
            title={`${meta.higherIsBetter ? "higher" : "lower"} is better`}
          >
            {crewBetter ? "▲" : "▼"} {Math.abs(imp).toFixed(0)}%
          </span>
        )}
      </div>

      <div className="relative mt-3.5 space-y-2">
        {/* The gate tick spans both bars so the eye reads "crew clears it". */}
        {showTick && (
          <span
            className="pointer-events-none absolute bottom-0 top-0 z-10 w-px bg-white/40"
            style={{ left: `calc(3.2rem + 0.625rem + (100% - 3.2rem - 0.625rem - 5.5rem - 0.625rem) * ${tickFraction})` }}
            aria-hidden
          />
        )}
        <ArmBar
          arm="Crew"
          value={pair.crew}
          std={spread.crew}
          fraction={barFraction(pair.crew, domainMax)}
          winner={crewBetter}
          format={meta.format}
        />
        <ArmBar
          arm="Baseline"
          value={pair.baseline}
          std={spread.baseline}
          fraction={barFraction(pair.baseline, domainMax)}
          winner={!crewBetter}
          format={meta.format}
        />
      </div>

      <p className="mt-2.5 flex items-center gap-1.5 text-[10.5px] text-white/40">
        <span className="uppercase tracking-wider">
          {meta.higherIsBetter ? "Higher is better" : "Lower is better"}
        </span>
        {gate !== null && threshold !== null && (
          <>
            <span className="text-white/20">·</span>
            <span className={gate ? "text-emerald-300/90" : "text-rose-300/90"}>
              gate {meta.higherIsBetter ? "≥" : "≤"} {meta.format(threshold)} {gate ? "✓" : "✗"}
            </span>
          </>
        )}
      </p>
    </div>
  );
}

/**
 * The headline §13 proof: the crew + shared canon against a single-agent,
 * no-memory baseline on the same demo sequence, as four side-by-side bar tiles
 * (CCS, accepted-footage efficiency, regeneration rate, style drift). Each tile
 * shows the relative improvement, the spread across runs (±σ), and whether the
 * crew clears the pre-registered gate.
 */
export function CrewVsBaselineCard({ report }: { report: EvalReport }) {
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-white/55">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full bg-gradient-to-r from-ember-deep to-ember-glow" />
          Crew + shared canon
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full bg-white/25" />
          Single-agent baseline (no memory)
        </span>
        <span className="text-white/35">
          mean over {report.runs} run{report.runs === 1 ? "" : "s"}, same book · seeds · prompts
        </span>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {METRICS.map((meta) => (
          <MetricTile
            key={meta.key}
            meta={meta}
            pair={report[meta.key]}
            spread={report.spread[meta.key]}
            report={report}
          />
        ))}
      </div>
    </div>
  );
}
