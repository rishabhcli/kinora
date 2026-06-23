import { useState } from "react";

import { type BufferHealthSummary, type EvalReport, summarizeReport } from "@kinora/core";

/**
 * A paste-ready, monospace summary of the §13 proof for a demo slide or the
 * submission writeup — the headline crew-vs-baseline numbers plus the committed
 * buffer's health — behind a one-click Copy.
 */
export function DemoSummaryBlock({
  report,
  health,
}: {
  report: EvalReport;
  health: BufferHealthSummary | null;
}) {
  const [copied, setCopied] = useState(false);
  const text = summarizeReport(report, health);

  const copy = () => {
    void navigator.clipboard?.writeText(text).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1600);
      },
      () => undefined,
    );
  };

  return (
    <div className="rounded-xl border border-white/8 bg-black/25">
      <div className="flex items-center justify-between border-b border-white/8 px-3 py-2">
        <span className="text-[10.5px] uppercase tracking-wider text-white/45">Copy for slides</span>
        <button
          type="button"
          onClick={copy}
          aria-label="Copy summary"
          className={`no-drag inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow ${
            copied ? "bg-emerald-400/20 text-emerald-200" : "bg-white/8 text-white/80 hover:bg-white/16"
          }`}
        >
          {copied ? (
            <>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="m5 13 4 4L19 7" />
              </svg>
              Copied
            </>
          ) : (
            <>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="9" y="9" width="11" height="11" rx="2" />
                <path d="M5 15V5a2 2 0 0 1 2-2h10" />
              </svg>
              Copy
            </>
          )}
        </button>
      </div>
      <pre className="overflow-x-auto whitespace-pre px-3.5 py-3 font-mono text-[11.5px] leading-relaxed text-white/75 selection:bg-ember-glow/30">
        {text}
      </pre>
    </div>
  );
}
