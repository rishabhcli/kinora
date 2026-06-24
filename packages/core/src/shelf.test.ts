import { describe, expect, it } from "vitest";

import type { BookResponse } from "./api/types";
import {
  applyIngestProgress,
  booksNeedingSync,
  canOpenBook,
  displayBookTitle,
  importGateMessage,
  stageLabel,
} from "./shelf";

const book = (overrides: Partial<BookResponse> = {}): BookResponse => ({
  id: "b1",
  title: "The Frog-King",
  author: "Brothers Grimm",
  status: "importing",
  stage: "analyse",
  progress: 0.2,
  num_pages: null,
  art_direction: null,
  created_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

describe("shelf helpers", () => {
  it("detects books that still need sync", () => {
    expect(booksNeedingSync([book(), book({ id: "b2", status: "ready" })])).toBe(true);
    expect(booksNeedingSync([book({ status: "ready" })])).toBe(false);
  });

  it("merges ingest progress into the cached shelf list", () => {
    const books = [book()];
    const next = applyIngestProgress(books, "b1", { stage: "shot_list", progress: 0.55 });
    expect(next?.[0]?.stage).toBe("shot_list");
    expect(next?.[0]?.progress).toBe(0.55);
    expect(applyIngestProgress(books, "b1", { stage: "analyse", progress: 0.2 })).toBe(books);
  });

  it("formats titles and gate copy", () => {
    expect(displayBookTitle("Demo (e2e seed)")).toBe("Demo");
    expect(displayBookTitle("little red riding hood")).toBe("Little Red Riding Hood");
    expect(stageLabel(book({ status: "failed" }))).toBe("Import failed");
    expect(importGateMessage(book())).toContain("20%");
    expect(canOpenBook(book({ status: "ready" }))).toBe(true);
    expect(canOpenBook(book())).toBe(false);
  });
});
