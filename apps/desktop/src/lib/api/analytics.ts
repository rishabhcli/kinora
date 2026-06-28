// Reading-analytics (Director domain) — derives a reading dashboard (pace, time,
// completion, streaks) from two sources:
//   1. The backend library (per-book completion %, live-film status).
//   2. A local, append-only *reading-event log* (session ticks: words read +
//      wall-clock seconds), since the backend exposes no per-reader analytics
//      endpoint yet. The log is the honest first-class data source; when a
//      backend `/me/analytics` lands, `summarize()` swaps its input with no UI
//      change (see DESIGN.md cross-domain notes).
//
// All derivation is PURE. The event store is an injectable KV (mirrors
// lib/settings.ts) so the math is testable with no DOM and no clock.
import type { LibraryBook } from "./library";

// ---- The reading-event log ------------------------------------------------ //

/** One reading-progress sample. Emitted by the reading room as the reader moves
 *  through a book; we only keep the deltas needed for pace/time/streak math. */
export interface ReadingEvent {
  book_id: string;
  /** Epoch ms when the sample was taken. */
  at: number;
  /** Words advanced since the previous sample for this session (>= 0). */
  words: number;
  /** Wall-clock seconds the reader was actually reading in this sample (> 0). */
  seconds: number;
}

const STORAGE_KEY = "kinora.reading-events.v1";
const MAX_EVENTS = 5000; // ring-buffer cap so the log can't grow unbounded

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function browserStore(): KeyValueStore | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    /* unavailable */
  }
  return null;
}

function parseEvents(raw: string | null): ReadingEvent[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: ReadingEvent[] = [];
  for (const row of parsed) {
    if (typeof row !== "object" || row === null) continue;
    const r = row as Record<string, unknown>;
    if (typeof r.book_id !== "string") continue;
    if (typeof r.at !== "number" || !Number.isFinite(r.at)) continue;
    const words = typeof r.words === "number" && Number.isFinite(r.words) ? Math.max(0, r.words) : 0;
    const seconds =
      typeof r.seconds === "number" && Number.isFinite(r.seconds) ? Math.max(0, r.seconds) : 0;
    out.push({ book_id: r.book_id, at: r.at, words, seconds });
  }
  return out;
}

export interface AnalyticsStore {
  events(): ReadingEvent[];
  /** Append a reading sample (clamped/validated). No-op for non-positive time. */
  record(event: ReadingEvent): void;
  clear(): void;
  subscribe(fn: () => void): () => void;
}

export function createAnalyticsStore(backing?: KeyValueStore): AnalyticsStore {
  const store = backing ?? browserStore();
  let events: ReadingEvent[] = parseEvents(store ? store.getItem(STORAGE_KEY) : null);
  const subs = new Set<() => void>();

  const persist = () => {
    try {
      store?.setItem(STORAGE_KEY, JSON.stringify(events));
    } catch {
      /* write blocked */
    }
    subs.forEach((fn) => fn());
  };

  return {
    events: () => events,
    record(event) {
      if (!(event.seconds > 0) || typeof event.book_id !== "string" || !event.book_id) return;
      const clean: ReadingEvent = {
        book_id: event.book_id,
        at: Number.isFinite(event.at) ? event.at : Date.now(),
        words: Math.max(0, Math.round(event.words)),
        seconds: Math.max(0, event.seconds),
      };
      events = [...events, clean].slice(-MAX_EVENTS);
      persist();
    },
    clear() {
      if (!events.length) return;
      events = [];
      persist();
    },
    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}

// ---- Pure derivation: pace, time, completion, streaks --------------------- //

const SECONDS_PER_DAY = 86_400;
const WORDS_PER_MINUTE_FLOOR = 1; // guard against div-by-zero / degenerate math

/** A local-day key (UTC) for streak/day bucketing — deterministic, clock-free. */
export function dayKey(epochMs: number): string {
  const day = Math.floor(epochMs / 1000 / SECONDS_PER_DAY);
  return String(day);
}

export interface PerBookStat {
  book_id: string;
  title: string;
  words: number;
  seconds: number;
  wpm: number;
  progress: number; // 0..100 (from the library)
  sessions: number; // distinct days touched (a rough session count)
}

export interface AnalyticsSummary {
  totalWords: number;
  totalSeconds: number;
  /** Average words-per-minute across all recorded reading time. */
  avgWpm: number;
  booksStarted: number;
  booksFinished: number;
  /** Completion rate over started books, 0..1. */
  completionRate: number;
  /** Consecutive days (ending at `now`) with at least one reading event. */
  currentStreakDays: number;
  longestStreakDays: number;
  daysActive: number;
  perBook: PerBookStat[];
}

/** Words-per-minute from words + seconds, guarded. */
export function wpm(words: number, seconds: number): number {
  if (seconds <= 0) return 0;
  const minutes = seconds / 60;
  return Math.round(words / Math.max(WORDS_PER_MINUTE_FLOOR / 60, minutes));
}

function streaks(activeDays: Set<string>, now: number): { current: number; longest: number } {
  if (!activeDays.size) return { current: 0, longest: 0 };
  const days = [...activeDays].map(Number).sort((a, b) => a - b);
  // longest run of consecutive day-indices
  let longest = 1;
  let run = 1;
  for (let i = 1; i < days.length; i++) {
    if (days[i] === days[i - 1] + 1) {
      run += 1;
      longest = Math.max(longest, run);
    } else {
      run = 1;
    }
  }
  // current streak: count back from today (or yesterday, if today is empty)
  const today = Math.floor(now / 1000 / SECONDS_PER_DAY);
  const set = new Set(days);
  let current = 0;
  let cursor = today;
  if (!set.has(cursor) && set.has(cursor - 1)) cursor -= 1; // grace: still "on streak" until tomorrow
  while (set.has(cursor)) {
    current += 1;
    cursor -= 1;
  }
  return { current, longest };
}

/** Build the full reading dashboard from the event log + the library shelf. */
export function summarize(
  events: ReadingEvent[],
  books: LibraryBook[],
  now: number = Date.now(),
): AnalyticsSummary {
  const titleById = new Map(books.map((b) => [b.id, b.title] as const));
  const progressById = new Map(books.map((b) => [b.id, b.progress] as const));

  const perBookMap = new Map<string, PerBookStat & { days: Set<string> }>();
  const activeDays = new Set<string>();
  let totalWords = 0;
  let totalSeconds = 0;

  for (const e of events) {
    totalWords += e.words;
    totalSeconds += e.seconds;
    activeDays.add(dayKey(e.at));
    let row = perBookMap.get(e.book_id);
    if (!row) {
      row = {
        book_id: e.book_id,
        title: titleById.get(e.book_id) ?? e.book_id,
        words: 0,
        seconds: 0,
        wpm: 0,
        progress: progressById.get(e.book_id) ?? 0,
        sessions: 0,
        days: new Set<string>(),
      };
      perBookMap.set(e.book_id, row);
    }
    row.words += e.words;
    row.seconds += e.seconds;
    row.days.add(dayKey(e.at));
  }

  const perBook: PerBookStat[] = [...perBookMap.values()]
    .map((r) => ({
      book_id: r.book_id,
      title: r.title,
      words: r.words,
      seconds: r.seconds,
      wpm: wpm(r.words, r.seconds),
      progress: r.progress,
      sessions: r.days.size,
    }))
    .sort((a, b) => b.seconds - a.seconds);

  // Completion is read from the library (authoritative progress), not the log:
  // a started book has progress > 0; a finished book is at 100.
  const booksStarted = books.filter((b) => b.progress > 0).length;
  const booksFinished = books.filter((b) => b.progress >= 100).length;
  const { current, longest } = streaks(activeDays, now);

  return {
    totalWords,
    totalSeconds,
    avgWpm: wpm(totalWords, totalSeconds),
    booksStarted,
    booksFinished,
    completionRate: booksStarted ? booksFinished / booksStarted : 0,
    currentStreakDays: current,
    longestStreakDays: longest,
    daysActive: activeDays.size,
    perBook,
  };
}

/** Human-readable duration (e.g. "2h 14m", "47s") for the dashboard cards. */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return remM ? `${h}h ${remM}m` : `${h}h`;
}

/** A daily reading-minutes series for a sparkline/bar chart, covering the last
 *  `days` days ending today (zero-filled). Returns oldest-first. */
export interface DayBucket {
  day: string;
  minutes: number;
  words: number;
}
export function dailySeries(events: ReadingEvent[], days = 14, now: number = Date.now()): DayBucket[] {
  const today = Math.floor(now / 1000 / SECONDS_PER_DAY);
  const buckets = new Map<number, { seconds: number; words: number }>();
  for (let d = today - days + 1; d <= today; d++) buckets.set(d, { seconds: 0, words: 0 });
  for (const e of events) {
    const d = Math.floor(e.at / 1000 / SECONDS_PER_DAY);
    const b = buckets.get(d);
    if (b) {
      b.seconds += e.seconds;
      b.words += e.words;
    }
  }
  return [...buckets.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([day, v]) => ({ day: String(day), minutes: Math.round(v.seconds / 60), words: v.words }));
}

/** One cell of a calendar heatmap: a day + an intensity level 0..4. */
export interface HeatmapCell {
  day: number; // day index (epoch days)
  minutes: number;
  level: 0 | 1 | 2 | 3 | 4;
}

/** Bucket reading minutes into a GitHub-style intensity level. Thresholds are
 *  relative to the busiest day in the window, so the scale auto-adapts. */
function intensity(minutes: number, max: number): HeatmapCell["level"] {
  if (minutes <= 0 || max <= 0) return 0;
  const ratio = minutes / max;
  if (ratio > 0.75) return 4;
  if (ratio > 0.5) return 3;
  if (ratio > 0.25) return 2;
  return 1;
}

/** A weeks × 7 heatmap grid ending today. `weeks` columns, each a Sun..Sat
 *  column of cells (older weeks first). Days with no reading are level 0. */
export function readingHeatmap(
  events: ReadingEvent[],
  weeks = 12,
  now: number = Date.now(),
): HeatmapCell[][] {
  const today = Math.floor(now / 1000 / SECONDS_PER_DAY);
  const minutesByDay = new Map<number, number>();
  for (const e of events) {
    const d = Math.floor(e.at / 1000 / SECONDS_PER_DAY);
    minutesByDay.set(d, (minutesByDay.get(d) ?? 0) + e.seconds / 60);
  }
  // Anchor the grid so the last column ends on today; align columns to weeks.
  const totalDays = weeks * 7;
  const start = today - totalDays + 1;
  let max = 0;
  for (let d = start; d <= today; d++) max = Math.max(max, minutesByDay.get(d) ?? 0);
  const columns: HeatmapCell[][] = [];
  for (let w = 0; w < weeks; w++) {
    const col: HeatmapCell[] = [];
    for (let dow = 0; dow < 7; dow++) {
      const day = start + w * 7 + dow;
      const minutes = Math.round(minutesByDay.get(day) ?? 0);
      col.push({ day, minutes, level: intensity(minutes, max) });
    }
    columns.push(col);
  }
  return columns;
}
