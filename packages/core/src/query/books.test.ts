import { describe, expect, it } from "vitest";

import type { BookResponse } from "../api/types";
import {
  booksNeedPolling,
  ingestProgressPercent,
  ingestStageLabel,
  isBookImporting,
  uploadErrorMessage,
} from "./books";

function book(overrides: Partial<BookResponse> = {}): BookResponse {
  return {
    id: "b1",
    title: "Test",
    status: "ready",
    ...overrides,
  };
}

describe("books query helpers", () => {
  it("detects importing books for polling", () => {
    expect(isBookImporting(book({ status: "importing" }))).toBe(true);
    expect(isBookImporting(book({ status: "ready" }))).toBe(false);
    expect(booksNeedPolling([book({ status: "ready" }), book({ status: "importing" })])).toBe(true);
    expect(booksNeedPolling([book({ status: "ready" })])).toBe(false);
    expect(booksNeedPolling(undefined)).toBe(false);
  });

  it("formats ingest stage labels", () => {
    expect(ingestStageLabel(book({ status: "failed" }))).toBe("Import failed");
    expect(ingestStageLabel(book({ status: "importing", stage: "canon_lock" }))).toBe("Canon lock");
    expect(ingestStageLabel(book({ status: "importing" }))).toBe("Preparing");
  });

  it("normalises progress to whole percents", () => {
    expect(ingestProgressPercent(book({ progress: 0.42 }))).toBe(42);
    expect(ingestProgressPercent(book({ progress: null }))).toBeNull();
    expect(ingestProgressPercent(book({ progress: 1.4 }))).toBe(100);
  });

  it("maps upload API errors to friendly copy", async () => {
    const tooMany = new Response(
      JSON.stringify({
        error: { type: "too_many_pages", message: "document exceeds the per-book page limit", detail: { max_pages: 300 } },
      }),
      { status: 413 },
    );
    await expect(uploadErrorMessage(tooMany)).resolves.toMatch(/300/);

    const quota = new Response(
      JSON.stringify({ error: { type: "book_quota_exceeded", message: "per-user book limit reached", detail: { max_books: 5 } } }),
      { status: 429 },
    );
    await expect(uploadErrorMessage(quota)).resolves.toMatch(/5-book/);
  });
});
