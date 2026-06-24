import { describe, expect, it } from "vitest";

import type { BookResponse } from "../api/types";
import {
  bookIsOpenable,
  bookProgressPercent,
  bookStageLabel,
  booksNeedPolling,
  booksRefetchInterval,
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
  it("polls while any book is importing", () => {
    expect(booksNeedPolling(undefined)).toBe(false);
    expect(booksNeedPolling([book({ status: "ready" })])).toBe(false);
    expect(booksNeedPolling([book({ status: "importing" })])).toBe(true);
    expect(booksRefetchInterval([book({ status: "importing" })])).toBe(2000);
    expect(booksRefetchInterval([book({ status: "ready" })])).toBe(false);
  });

  it("formats stage labels and progress", () => {
    expect(bookStageLabel(book({ status: "failed" }))).toBe("Import failed");
    expect(bookStageLabel(book({ stage: "extracting_pages" }))).toBe("Extracting pages");
    expect(bookProgressPercent(0.42)).toBe(42);
    expect(bookProgressPercent(72)).toBe(72);
    expect(bookProgressPercent(null)).toBeNull();
  });

  it("only allows opening ready books", () => {
    expect(bookIsOpenable(book({ status: "ready" }))).toBe(true);
    expect(bookIsOpenable(book({ status: "importing" }))).toBe(false);
  });
});
