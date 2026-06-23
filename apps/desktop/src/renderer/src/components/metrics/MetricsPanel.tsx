import {
  type BufferPoint,
  type ErrorEnvelope,
  type EvalReport,
  bufferHealth,
  isEvalReport,
  queryKeys,
} from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../lib/api";
import { BufferSawtoothChart } from "./BufferSawtoothChart";
import { CrewVsBaselineCard } from "./CrewVsBaselineCard";
import { DemoSummaryBlock } from "./DemoSummaryBlock";
import { MetricsExportBar } from "./MetricsExportBar";
import { PerCharacterCcsTable } from "./PerCharacterCcsTable";
import { VerdictBanner } from "./VerdictBanner";

interface MetricsPanelProps {
  bookId: string;
  sessionId: string | null;
  bookTitle?: string | null;
  /** A changing reading-progress signal (e.g. the focus word) that, while the
   *  panel is open, debounce-triggers a live recompute of the buffer sawtooth. */
  liveSignal?: number;
  onClose: () => void;
}

/** The report load resolves to either the cached report or a "not ready" reason
 *  (almost always the §13 cache miss) — modelled as a value, not a thrown error,
 *  so it drives the run-eval empty state without react-query retry churn. */
type ReportResult =
  | { ok: true; report: EvalReport }
  | { ok: false; notReady: boolean; message: string };

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <p className="mb-2.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-white/45">
      {children}
    </p>
  );
}

/** A copyable shell command (the exact CLI the backend's 404 points the operator to). */
function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-black/35 px-3 py-2">
      <code className="flex-1 overflow-x-auto whitespace-nowrap font-mono text-[12px] text-ember-glow/90">
        {command}
      </code>
      <button
        type="button"
        aria-label="Copy command"
        onClick={() => {
          void navigator.clipboard?.writeText(command).then(
            () => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            },
            () => undefined,
          );
        }}
        className="no-drag shrink-0 rounded-md bg-white/10 px-2 py-1 text-[11px] font-medium text-white/80 transition hover:bg-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

/** Shown when no §13 report has been cached for this book yet — the buffer
 *  sawtooth below still works live; only the crew-vs-baseline proof needs the CLI. */
function RunEvalEmptyState({
  bookId,
  message,
  onRetry,
}: {
  bookId: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="rounded-2xl border border-dashed border-white/15 bg-white/[0.02] p-6 text-center">
      <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-full bg-ember/15 text-ember-glow">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 21V10M12 21V4M19 21v-7" />
        </svg>
      </div>
      <p className="font-display text-[15px] font-semibold text-white">No eval report cached yet</p>
      <p className="mx-auto mt-1.5 max-w-md text-[12.5px] leading-relaxed text-white/50">{message}</p>
      <div className="mx-auto mt-4 max-w-md text-left">
        <CopyCommand command={`python -m app.eval.run --book ${bookId}`} />
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="no-drag mt-4 inline-flex items-center gap-1.5 rounded-full bg-white/10 px-4 py-1.5 text-[12px] font-medium text-white/85 transition hover:bg-white/18 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
      >
        Retry
      </button>
    </div>
  );
}

/**
 * The §13 metrics panel: a full-screen frosted overlay that puts the Track-3
 * proof on one screen-recordable surface — the crew vs single-agent baseline
 * card, the per-character CCS table, the live committed-buffer sawtooth, and a
 * copy-friendly slide summary. Opened from the reading toolbar (book + session
 * context). Closes on Escape or an outside click.
 */
export function MetricsPanel({
  bookId,
  sessionId,
  bookTitle,
  liveSignal,
  onClose,
}: MetricsPanelProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [velocity, setVelocity] = useState<number | null>(null);

  // Escape to close, a Tab focus-trap, focus-on-open, and focus restored on
  // unmount — a proper modal dialog for keyboard + screen-reader users.
  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => !el.hasAttribute("disabled") && el.offsetParent !== null);
      const firstEl = focusable[0];
      const lastEl = focusable[focusable.length - 1];
      if (!firstEl || !lastEl) return;
      const active = document.activeElement;
      if (event.shiftKey && active === firstEl) {
        event.preventDefault();
        lastEl.focus();
      } else if (!event.shiftKey && active === lastEl) {
        event.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previouslyFocused?.focus?.();
    };
  }, [onClose]);

  const reportQuery = useQuery({
    queryKey: queryKeys.evalReport(bookId),
    staleTime: 0,
    retry: false,
    queryFn: async (): Promise<ReportResult> => {
      const { data, error, response } = await api.GET("/api/eval/report/{book_id}", {
        params: { path: { book_id: bookId } },
      });
      if (data && isEvalReport(data)) return { ok: true, report: data };
      const message =
        (error as unknown as Partial<ErrorEnvelope> | undefined)?.error?.message ??
        "Could not load the eval report.";
      return { ok: false, notReady: response?.status === 404, message };
    },
  });

  const bufferQuery = useQuery({
    // Velocity is part of the key so dragging the speed slider refetches with
    // the new sim param (and caches each speed independently).
    queryKey: [...queryKeys.bufferTrace(sessionId ?? ""), velocity ?? "auto"],
    enabled: Boolean(sessionId),
    staleTime: 0,
    queryFn: async (): Promise<BufferPoint[]> => {
      const { data, error } = await api.GET("/api/eval/buffer-trace/{session_id}", {
        params: {
          path: { session_id: sessionId as string },
          query: { velocity: velocity ?? undefined },
        },
      });
      if (error || !data) throw new Error("buffer trace failed");
      return data;
    },
  });

  // Live recompute: when the reader advances (and the slider is on "auto"), pull
  // a fresh trace shortly after they stop moving, so the sawtooth evolves with
  // the read. A manual velocity override pins the sim, so we don't fight it.
  const refetchBufferRef = useRef(bufferQuery.refetch);
  refetchBufferRef.current = bufferQuery.refetch;
  useEffect(() => {
    if (!sessionId || liveSignal === undefined || velocity !== null) return;
    const id = window.setTimeout(() => void refetchBufferRef.current(), 1800);
    return () => window.clearTimeout(id);
  }, [liveSignal, sessionId, velocity]);

  const trace = useMemo(() => bufferQuery.data ?? [], [bufferQuery.data]);
  const health = useMemo(() => bufferHealth(trace), [trace]);
  const result = reportQuery.data;
  const report = result?.ok ? result.report : null;

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4 sm:p-8" role="presentation">
      <div className="absolute inset-0 bg-walnut-deep/70 backdrop-blur-xl" onClick={onClose} aria-hidden />

      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Metrics"
        tabIndex={-1}
        className="glass-strong no-drag relative flex max-h-[90vh] w-full max-w-[1000px] flex-col overflow-hidden rounded-glass text-white focus:outline-none"
      >
        {/* Header */}
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-white/10 px-6 py-4">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ember-glow/90">
              Proof · §13
            </p>
            <h2 className="mt-0.5 font-display text-[19px] font-semibold leading-tight text-white">
              Metrics
            </h2>
            <p className="mt-0.5 truncate text-[12px] text-white/45">
              Consistency is a memory problem — crew + canon vs a single agent
              {bookTitle ? ` · ${bookTitle}` : ""}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {report && <MetricsExportBar report={report} health={sessionId ? health : null} bookId={bookId} />}
            <button
              type="button"
              aria-label="Close metrics"
              onClick={onClose}
              className="toolbar-btn no-drag shrink-0"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                <path d="M6 6l12 12M18 6 6 18" />
              </svg>
            </button>
          </div>
        </header>

        {/* Scrollable body */}
        <div className="min-h-0 flex-1 space-y-7 overflow-y-auto px-6 py-5">
          {report && <VerdictBanner report={report} />}

          <section>
            <SectionLabel>Crew vs single-agent baseline</SectionLabel>
            {reportQuery.isLoading ? (
              <div className="shimmer h-44 rounded-2xl border border-white/8 bg-white/[0.02]" />
            ) : report ? (
              <CrewVsBaselineCard report={report} />
            ) : (
              <RunEvalEmptyState
                bookId={bookId}
                message={result && !result.ok ? result.message : "Run the eval CLI to produce the proof."}
                onRetry={() => void reportQuery.refetch()}
              />
            )}
          </section>

          <section>
            <SectionLabel>Committed-buffer occupancy · live</SectionLabel>
            <BufferSawtoothChart
              trace={trace}
              health={health}
              isLoading={bufferQuery.isLoading && Boolean(sessionId)}
              isFetching={bufferQuery.isFetching}
              isError={bufferQuery.isError}
              sessionReady={Boolean(sessionId)}
              aboveLowTarget={report?.thresholds.buffer_above_low_target ?? 0.99}
              onRefresh={() => void bufferQuery.refetch()}
              velocity={velocity}
              onVelocityChange={setVelocity}
            />
          </section>

          {report && (
            <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <div>
                <SectionLabel>Per-character consistency</SectionLabel>
                <PerCharacterCcsTable report={report} />
              </div>
              <div>
                <SectionLabel>Demo summary</SectionLabel>
                <DemoSummaryBlock report={report} health={sessionId ? health : null} />
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
