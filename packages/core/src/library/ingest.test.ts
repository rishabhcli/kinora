import { describe, expect, it } from "vitest";

import { type BookResponse } from "../api/types";
import {
  applyIngestProgress,
  bookProgressPercent,
  bookStageLabel,
  patchBooksWithIngestEvent,
  shelfHasImporting,
} from "./ingest";

const book = (overrides: Partial<BookResponse> = {}): BookResponse => ({
  id: "b1",
  title: "Test",
  status: "importing",
  ...overrides,
});

describe("applyIngestProgress", () => {
  it("updates stage and progress for the matching book", () => {
    const next = applyIngestProgress(book(), {
      book_id: "b1",
      stage: "analyse",
      pct: 0.42,
    });
    expect(next.stage).toBe("analyse");
    expect(next.progress).toBe(0.42);
  });

  it("leaves other books untouched", () => {
    const row = book({ id: "other" });
    expect(applyIngestProgress(row, { book_id: "b1", stage: "x", pct: 1 })).toBe(row);
  });
});

describe("patchBooksWithIngestEvent", () => {
  it("patches the list for ingest_progress", () => {
    const rows = [book({ id: "a" }), book({ id: "b" })];
    const next = patchBooksWithIngestEvent(rows, {
      event: "ingest_progress",
      book_id: "b",
      stage: "shots",
      pct: 0.8,
    });
    expect(next?.[1]?.stage).toBe("shots");
    expect(next?.[1]?.progress).toBe(0.8);
    expect(next?.[0]?.progress).toBeUndefined();
  });
});

describe("shelfHasImporting", () => {
  it("detects importing rows", () => {
    expect(shelfHasImporting([book({ status: "ready" }), book({ status: "importing" })])).toBe(true);
    expect(shelfHasImporting([book({ status: "ready" })])).toBe(false);
  });
});

describe("bookStageLabel", () => {
  it("formats stages and failures", () => {
    expect(bookStageLabel(book({ stage: "identity_lock" }))).toBe("Identity lock");
    expect(bookStageLabel(book({ status: "failed" }))).toBe("Import failed");
    expect(bookStageLabel(book())).toBe("Preparing");
  });
});

describe("bookProgressPercent", () => {
  it("clamps and rounds", () => {
    expect(bookProgressPercent(book({ progress: 0.426 }))).toBe(43);
    expect(bookProgressPercent(book({ progress: null }))).toBeNull();
  });
});
