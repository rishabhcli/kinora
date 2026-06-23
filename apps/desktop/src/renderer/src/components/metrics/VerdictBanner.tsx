import { type EvalReport, reportVerdict } from "@kinora/core";

/**
 * The headline §13 verdict — the single most persuasive line for a judge: how
 * many metrics the crew wins and how many pre-registered gates it clears, with
 * the honesty callout that the thresholds were frozen before the run. Reads
 * emerald on a clean sweep, amber when the win is partial.
 */
export function VerdictBanner({ report }: { report: EvalReport }) {
  const verdict = reportVerdict(report);
  const strong = verdict.sweep && verdict.gatesMet === verdict.gatesTotal;

  return (
    <div
      className={`relative overflow-hidden rounded-2xl border p-4 ${
        strong ? "border-emerald-400/25 bg-emerald-400/[0.07]" : "border-amber-400/25 bg-amber-400/[0.06]"
      }`}
    >
      <div className="flex items-start gap-3.5">
        <span
          className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${
            strong ? "bg-emerald-400/20 text-emerald-300" : "bg-amber-400/20 text-amber-300"
          }`}
          aria-hidden
        >
          {strong ? (
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M6 9V4h12v5a6 6 0 0 1-12 0Z" />
              <path d="M6 5H3.5a2.5 2.5 0 0 0 4 2M18 5h2.5a2.5 2.5 0 0 1-4 2M9 21h6M12 15v6" />
            </svg>
          ) : (
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
            </svg>
          )}
        </span>
        <div className="min-w-0">
          <p className="font-display text-[15px] font-semibold leading-snug text-white">
            {verdict.headline}
          </p>
          <p className="mt-1 text-[12px] leading-relaxed text-white/55">
            Same book, seeds and prompts in both arms — only memory + the crew differ. Thresholds
            were <span className="text-white/80">pre-registered (§9.5)</span> before the run, so they
            can&rsquo;t be tuned to flatter the result.
          </p>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] tabular-nums text-white/55">
            <span>
              CCS <b className="text-white">{report.ccs.crew.toFixed(3)}</b> vs{" "}
              {report.ccs.baseline.toFixed(3)}
            </span>
            <span>
              Efficiency <b className="text-white">{report.efficiency.crew.toFixed(1)}%</b> vs{" "}
              {report.efficiency.baseline.toFixed(1)}%
            </span>
            <span className="text-white/40">
              {report.runs} run{report.runs === 1 ? "" : "s"}, mean ± σ
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
