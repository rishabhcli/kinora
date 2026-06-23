import { describe, expect, it } from "vitest";

import type { BookResponse } from "./api/types";
import {
  applyIngestProgress,
  formatIngestStage,
  importGateMessage,
  patchBooksWithIngest,
  uploadErrorMessage,
} from "./shelf";

const baseBook: BookResponse = {
  id: "book-1",
  title: "Demo",
  status: "importing",
  author: null,
  num_pages: null,
  art_direction: null,
  created_at: null,
  progress: 0.1,
  stage: "extract",
};

describe("formatIngestStage", () => {
  it("title-cases slug stages", () => {
    expect(formatIngestStage("shot_plan")).toBe("Shot plan");
  });
});

describe("importGateMessage", () => {
  it("returns null for ready books", () => {
    expect(importGateMessage({ ...baseBook, status: "ready" })).toBeNull();
  });

  it("includes percent for importing books", () => {
    const msg = importGateMessage({ ...baseBook, progress: 0.42, stage: "analyze" });
    expect(msg).toContain("42%");
    expect(msg).toContain("Analyze");
  });
});

describe("applyIngestProgress", () => {
  it("updates stage and progress for the matching book", () => {
    const next = applyIngestProgress(baseBook, {
      book_id: "book-1",
      stage: "canon",
      pct: 0.6,
    });
    expect(next.stage).toBe("canon");
    expect(next.progress).toBe(0.6);
    expect(next.status).toBe("importing");
  });

  it("flips status to ready on the ready stage", () => {
    const next = applyIngestProgress(baseBook, {
      book_id: "book-1",
      stage: "ready",
      pct: 1,
    });
    expect(next.status).toBe("ready");
  });

  it("flips status to failed on the failed stage", () => {
    const next = applyIngestProgress(baseBook, {
      book_id: "book-1",
      stage: "failed",
      pct: 1,
    });
    expect(next.status).toBe("failed");
  });
});

describe("patchBooksWithIngest", () => {
  it("patches only the targeted book", () => {
    const other: BookResponse = { ...baseBook, id: "book-2" };
    const next = patchBooksWithIngest([baseBook, other], {
      book_id: "book-1",
      stage: "ready",
      pct: 1,
    });
    expect(next?.[0]?.status).toBe("ready");
    expect(next?.[1]?.status).toBe("importing");
  });
});

describe("uploadErrorMessage", () => {
  it("reads the API error envelope", () => {
    expect(
      uploadErrorMessage({ error: { type: "invalid_pdf", message: "not a PDF" } }),
    ).toBe("not a PDF");
  });
});
