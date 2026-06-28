import { describe, it, expect } from "vitest";
import type { LibraryBook } from "./library";
import {
  createAnalyticsStore,
  summarize,
  wpm,
  formatDuration,
  dailySeries,
  dayKey,
  readingHeatmap,
  type KeyValueStore,
  type ReadingEvent,
} from "./analytics";

const DAY = 86_400_000;
const memStore = (seed?: Record<string, string>): KeyValueStore => {
  const m = new Map(Object.entries(seed ?? {}));
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
};

function book(over: Partial<LibraryBook> = {}): LibraryBook {
  return {
    id: over.id ?? "id",
    title: over.title ?? "Title",
    author: "A",
    progress: over.progress ?? 0,
    coverColor: "#000",
    coverGradient: "g",
    coverImage: "",
    textColor: "#fff",
    spineColor: "#000",
  };
}

describe("wpm + formatDuration", () => {
  it("computes words per minute, guarded against zero time", () => {
    expect(wpm(300, 60)).toBe(300);
    expect(wpm(150, 30)).toBe(300);
    expect(wpm(100, 0)).toBe(0);
  });
  it("formats durations", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(120)).toBe("2m");
    expect(formatDuration(3600)).toBe("1h");
    expect(formatDuration(3660 + 540)).toBe("1h 10m");
  });
});

describe("analytics store", () => {
  it("records, clamps, persists, notifies", () => {
    const backing = memStore();
    const store = createAnalyticsStore(backing);
    let notified = 0;
    store.subscribe(() => notified++);

    store.record({ book_id: "a", at: 1000, words: 100, seconds: 60 });
    store.record({ book_id: "a", at: 2000, words: -5, seconds: 0 }); // dropped (no time)
    expect(store.events()).toHaveLength(1);
    expect(notified).toBe(1);

    // negative words clamp to 0; persisted + rehydrated
    store.record({ book_id: "a", at: 3000, words: -10, seconds: 30 });
    expect(store.events()[1].words).toBe(0);
    expect(createAnalyticsStore(backing).events()).toHaveLength(2);
  });
});

describe("summarize", () => {
  const now = 10 * DAY + 12 * 3600_000;
  const events: ReadingEvent[] = [
    { book_id: "a", at: 8 * DAY, words: 600, seconds: 120 },
    { book_id: "a", at: 9 * DAY, words: 300, seconds: 60 },
    { book_id: "b", at: 10 * DAY, words: 150, seconds: 30 },
  ];
  const books = [
    book({ id: "a", title: "A", progress: 100 }),
    book({ id: "b", title: "B", progress: 40 }),
    book({ id: "c", title: "C", progress: 0 }),
  ];

  it("totals words/seconds and average wpm", () => {
    const s = summarize(events, books, now);
    expect(s.totalWords).toBe(1050);
    expect(s.totalSeconds).toBe(210);
    expect(s.avgWpm).toBe(300);
  });

  it("derives completion from library progress, not the log", () => {
    const s = summarize(events, books, now);
    expect(s.booksStarted).toBe(2); // a + b have progress > 0
    expect(s.booksFinished).toBe(1); // a is 100
    expect(s.completionRate).toBeCloseTo(0.5);
  });

  it("computes consecutive-day streaks", () => {
    const s = summarize(events, books, now);
    // days 8,9,10 are consecutive and 10 is "today"
    expect(s.longestStreakDays).toBe(3);
    expect(s.currentStreakDays).toBe(3);
    expect(s.daysActive).toBe(3);
  });

  it("groups per-book stats sorted by time", () => {
    const s = summarize(events, books, now);
    expect(s.perBook[0].book_id).toBe("a");
    expect(s.perBook[0].words).toBe(900);
    expect(s.perBook[0].sessions).toBe(2);
  });
});

describe("dailySeries", () => {
  it("zero-fills a window ending today, oldest first", () => {
    const now = 5 * DAY;
    const events: ReadingEvent[] = [{ book_id: "a", at: 4 * DAY, words: 60, seconds: 120 }];
    const series = dailySeries(events, 3, now);
    expect(series).toHaveLength(3);
    expect(series.map((d) => d.minutes)).toEqual([0, 2, 0]); // days 3,4,5
    expect(Number(series[0].day)).toBeLessThan(Number(series[2].day));
  });
});

describe("dayKey", () => {
  it("is stable within a UTC day", () => {
    expect(dayKey(3 * DAY)).toBe(dayKey(3 * DAY + 3600_000));
    expect(dayKey(3 * DAY)).not.toBe(dayKey(4 * DAY));
  });
});

describe("readingHeatmap", () => {
  it("builds a weeks × 7 grid ending today with auto-scaled levels", () => {
    const now = 30 * DAY;
    const events: ReadingEvent[] = [
      { book_id: "a", at: 30 * DAY, words: 0, seconds: 60 * 60 }, // busiest day -> level 4
      { book_id: "a", at: 29 * DAY, words: 0, seconds: 15 * 60 }, // a quarter -> level 1
    ];
    const grid = readingHeatmap(events, 4, now);
    expect(grid).toHaveLength(4); // 4 week-columns
    expect(grid.every((col) => col.length === 7)).toBe(true);
    const cells = grid.flat();
    const today = cells.find((c) => c.day === 30);
    expect(today?.level).toBe(4);
    const yest = cells.find((c) => c.day === 29);
    expect(yest?.level).toBe(1);
    // days with no reading are level 0
    expect(cells.filter((c) => c.level === 0).length).toBeGreaterThan(0);
  });

  it("is all-zero with no events", () => {
    const grid = readingHeatmap([], 2, 10 * DAY);
    expect(grid.flat().every((c) => c.level === 0 && c.minutes === 0)).toBe(true);
  });
});
