import { describe, expect, it } from "vitest";

import type { BookResponse } from "./api/types";
import { applyIngestProgress, importGateMessage, progressPercent, shelfNeedsIngestUpdates, stageLabel } from "./shelf";

const book = (overrides: Partial<BookResponse> = {}): BookResponse => ({
  id: "b1",
  title: "Test",
  status: "importing",
  ...overrides,
});

describe("shelf helpers", () => {
  it("formats stage labels", () => {
    expect(stageLabel(book({ status: "failed" }))).toBe("Import failed");
    expect(stageLabel(book({ stage: "analyse_pages" }))).toBe("Analyse pages");
  });

  it("gates non-ready books", () => {
    expect(importGateMessage(book({ status: "ready" }))).toBeNull();
    expect(importGateMessage(book({ status: "failed" }))).toContain("failed");
    expect(importGateMessage(book({ stage: "canon", progress: 0.42 }))).toContain("42%");
  });

  it("merges ingest progress", () => {
    const next = applyIngestProgress(book(), { stage: "shots", pct: 0.5 });
    expect(next.stage).toBe("shots");
    expect(next.progress).toBe(0.5);
  });

  it("detects importing shelves", () => {
    expect(shelfNeedsIngestUpdates([book(), book({ status: "ready", id: "b2" })])).toBe(true);
    expect(shelfNeedsIngestUpdates([book({ status: "ready" })])).toBe(false);
  });

  it("clamps progress percent", () => {
    expect(progressPercent(book({ progress: 1.2 }))).toBe(100);
    expect(progressPercent(book({ progress: null }))).toBeNull();
  });
});
