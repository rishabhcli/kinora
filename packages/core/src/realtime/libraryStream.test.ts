import { describe, expect, it } from "vitest";

import {
  applyIngestProgress,
  LibraryEventStream,
  shelfHasImporting,
  type EventSourceLike,
} from "./libraryStream";
import type { BookResponse } from "../api/types";

function sampleBooks(): BookResponse[] {
  return [
    { id: "b1", title: "Ready", status: "ready" },
    { id: "b2", title: "Importing", status: "importing", stage: "extract", progress: 0.1 },
  ];
}

describe("applyIngestProgress", () => {
  it("patches stage and pct for the matching book", () => {
    const next = applyIngestProgress(sampleBooks(), {
      event: "ingest_progress",
      book_id: "b2",
      stage: "analyse",
      pct: 0.42,
    });
    expect(next?.[1]).toMatchObject({ stage: "analyse", progress: 0.42, status: "importing" });
    expect(next?.[0]).toMatchObject({ status: "ready" });
  });

  it("ignores events for unknown books", () => {
    const books = sampleBooks();
    expect(applyIngestProgress(books, { event: "ingest_progress", book_id: "missing", pct: 1 })).toBe(
      books,
    );
  });
});

describe("shelfHasImporting", () => {
  it("is true when any book is importing", () => {
    expect(shelfHasImporting(sampleBooks())).toBe(true);
    expect(shelfHasImporting([{ id: "x", title: "x", status: "ready" }])).toBe(false);
  });
});

describe("LibraryEventStream", () => {
  it("forwards ingest_progress SSE payloads to the callback", async () => {
    const listeners = new Map<string, (event: { data: string }) => void>();
    const factory = (_url: string): EventSourceLike => ({
      close: () => undefined,
      addEventListener: (type, fn) => {
        listeners.set(type, fn);
      },
      onopen: null,
      onerror: null,
    });

    const seen: unknown[] = [];
    const stream = new LibraryEventStream({
      baseUrl: "http://api.test",
      getToken: () => "tok",
      createEventSource: factory,
      onIngestProgress: (event) => seen.push(event),
      reconnect: false,
    });
    await stream.connect();
    listeners.get("ingest_progress")?.({
      data: JSON.stringify({ event: "ingest_progress", book_id: "b2", stage: "canon", pct: 0.8 }),
    });
    expect(seen[0]).toMatchObject({ book_id: "b2", stage: "canon", pct: 0.8 });
    stream.close();
  });
});
