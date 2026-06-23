import { type BufferHealthSummary, type EvalReport, reportToMarkdown } from "@kinora/core";
import { useState } from "react";

import { downloadText } from "./export";

/**
 * Export affordances for the §13 proof: download the raw report JSON and copy a
 * GitHub-flavoured Markdown table — both straight into a deck or the submission
 * writeup. (The buffer chart owns its own PNG export, next to its controls.)
 */
export function MetricsExportBar({
  report,
  health,
  bookId,
}: {
  report: EvalReport;
  health: BufferHealthSummary | null;
  bookId: string;
}) {
  const [copied, setCopied] = useState(false);

  const copyMarkdown = () => {
    void navigator.clipboard?.writeText(reportToMarkdown(report, health)).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1600);
      },
      () => undefined,
    );
  };

  const downloadJson = () => {
    downloadText(JSON.stringify(report, null, 2), `kinora-eval-${bookId}.json`, "application/json");
  };

  const btn =
    "no-drag inline-flex items-center gap-1.5 rounded-full bg-white/8 px-3 py-1.5 text-[11px] font-medium text-white/80 transition hover:bg-white/16 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow";

  return (
    <div className="flex items-center gap-2">
      <button type="button" onClick={copyMarkdown} className={btn}>
        {copied ? (
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="m5 13 4 4L19 7" />
          </svg>
        ) : (
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="9" y="9" width="11" height="11" rx="2" />
            <path d="M5 15V5a2 2 0 0 1 2-2h10" />
          </svg>
        )}
        {copied ? "Copied" : "Markdown"}
      </button>
      <button type="button" onClick={downloadJson} className={btn}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 3v12m0 0 4-4m-4 4-4-4M5 21h14" />
        </svg>
        JSON
      </button>
    </div>
  );
}
