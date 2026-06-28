// AnalyticsDashboard — the reading-analytics surface (§8.6 directing style +
// local reading-event analytics). Top cards: pace (avg wpm), time, completion,
// streak. A 14-day reading sparkline. Per-book stats. And the learned directing
// style (slower/warmer/wider biases) pulled from `GET /me/prefs` or a book's
// `GET /books/{id}/prefs`.
//
// All math comes from the pure `lib/api/analytics` helpers, driven by an
// injected AnalyticsStore (local event log) + the library shelf.
import { useEffect, useMemo, useState } from "react";
import type { LibraryBook } from "../../lib/api/library";
import {
  summarize,
  dailySeries,
  formatDuration,
  type AnalyticsStore,
} from "../../lib/api/analytics";
import { director, type DirectingStyle } from "../../lib/api/director";

interface AnalyticsDashboardProps {
  books: LibraryBook[];
  analytics: AnalyticsStore;
  /** When set, also load this book's directing style; else the user-wide style. */
  bookId?: string;
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-xl p-3.5" style={{ background: "rgba(255,255,255,0.035)", border: "1px solid rgba(255,255,255,0.07)" }}>
      <p className="text-[10px] uppercase tracking-wide text-kinora-muted">{label}</p>
      <p className="font-serif text-xl font-semibold text-kinora-text mt-1">{value}</p>
      {sub && <p className="text-[10px] text-kinora-muted mt-0.5">{sub}</p>}
    </div>
  );
}

function Sparkline({ values }: { values: number[] }) {
  const max = Math.max(1, ...values);
  return (
    <div className="flex items-end gap-1 h-16" aria-hidden>
      {values.map((v, i) => (
        <div
          key={i}
          className="flex-1 rounded-t"
          style={{
            height: `${Math.round((v / max) * 100)}%`,
            minHeight: v > 0 ? 3 : 1,
            background: v > 0 ? "linear-gradient(180deg, rgba(212,164,78,0.85), rgba(212,164,78,0.35))" : "rgba(255,255,255,0.06)",
          }}
        />
      ))}
    </div>
  );
}

function useAnalyticsTick(store: AnalyticsStore): number {
  const [tick, setTick] = useState(0);
  useEffect(() => store.subscribe(() => setTick((n) => n + 1)), [store]);
  return tick;
}

export default function AnalyticsDashboard({ books, analytics, bookId }: AnalyticsDashboardProps) {
  const tick = useAnalyticsTick(analytics);
  const summary = useMemo(() => summarize(analytics.events(), books), [analytics, books, tick]);
  const series = useMemo(() => dailySeries(analytics.events(), 14), [analytics, tick]);

  const [style, setStyle] = useState<DirectingStyle | null>(null);
  useEffect(() => {
    let alive = true;
    const load = bookId ? director.getBookStyle(bookId) : director.getMyStyle();
    load
      .then((s) => alive && setStyle(s))
      .catch(() => alive && setStyle(null));
    return () => {
      alive = false;
    };
  }, [bookId]);

  const completionPct = Math.round(summary.completionRate * 100);

  return (
    <div className="flex flex-col gap-5">
      {/* Top stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat label="Avg pace" value={`${summary.avgWpm}`} sub="words / min" />
        <Stat label="Time read" value={formatDuration(summary.totalSeconds)} sub={`${summary.totalWords.toLocaleString()} words`} />
        <Stat label="Completion" value={`${completionPct}%`} sub={`${summary.booksFinished}/${summary.booksStarted} finished`} />
        <Stat label="Streak" value={`${summary.currentStreakDays}d`} sub={`best ${summary.longestStreakDays}d · ${summary.daysActive} days active`} />
      </div>

      {/* 14-day sparkline */}
      <div className="rounded-xl p-3.5" style={{ background: "rgba(255,255,255,0.025)", border: "1px solid rgba(255,255,255,0.06)" }}>
        <p className="text-[11px] font-medium text-kinora-text mb-2">Last 14 days · reading minutes</p>
        <Sparkline values={series.map((d) => d.minutes)} />
      </div>

      {/* Directing style */}
      {style && style.priors.length > 0 && (
        <div className="rounded-xl p-3.5" style={{ background: "rgba(255,255,255,0.025)", border: "1px solid rgba(255,255,255,0.06)" }}>
          <p className="text-[11px] font-medium text-kinora-text mb-2">
            Your directing style {bookId ? "for this book" : "across all books"}
          </p>
          <ul className="flex flex-col gap-1.5">
            {style.priors.map((p) => (
              <li key={p.kind} className="flex items-center justify-between text-[11px]">
                <span className="text-kinora-text/90">
                  {p.label}
                  {p.applied && p.applied_value ? (
                    <span className="text-kinora-muted"> · defaults to {p.applied_value}</span>
                  ) : null}
                </span>
                <span className="text-[10px] text-kinora-muted tabular-nums">{p.detail}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Per-book */}
      {summary.perBook.length > 0 && (
        <div>
          <p className="text-[11px] font-medium text-kinora-text mb-2">By book</p>
          <ul className="flex flex-col gap-1.5">
            {summary.perBook.slice(0, 12).map((b) => (
              <li
                key={b.book_id}
                className="flex items-center justify-between rounded-lg px-3 py-2 text-[11px]"
                style={{ background: "rgba(255,255,255,0.025)", border: "1px solid rgba(255,255,255,0.05)" }}
              >
                <span className="text-kinora-text truncate flex-1">{b.title}</span>
                <span className="text-kinora-muted ml-3 shrink-0">
                  {formatDuration(b.seconds)} · {b.wpm} wpm · {b.progress}%
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {summary.totalSeconds === 0 && (
        <p className="text-[11px] text-kinora-muted text-center py-4">
          No reading recorded yet — your pace, time, and streaks will appear here as you read.
        </p>
      )}
    </div>
  );
}
