import { describe, expect, it } from "vitest";

import type { BookResponse } from "../api/types";
import {
  applyIngestProgress,
  formatIngestPercent,
  ingestStageLabel,
  LibraryEventSource,
  type EventSourceLike,
} from "./library";

const sampleBook = (overrides: Partial<BookResponse> = {}): BookResponse => ({
  id: "book_1",
  title: "Demo",
  status: "importing",
  stage: "extract",
  progress: 0.2,
  ...overrides,
});

describe("applyIngestProgress", () => {
  it("updates stage, progress, and maps ready/failed to status", () => {
    const books = [sampleBook()];
    const ready = applyIngestProgress(books, { book_id: "book_1", stage: "ready", pct: 1 });
    expect(ready[0]?.status).toBe("ready");
    expect(ready[0]?.stage).toBe("ready");
    expect(ready[0]?.progress).toBe(1);

    const failed = applyIngestProgress(books, { book_id: "book_1", stage: "failed", pct: 1 });
    expect(failed[0]?.status).toBe("failed");

    const other = applyIngestProgress(books, { book_id: "book_2", stage: "analyze", pct: 0.5 });
    expect(other).toEqual(books);
  });
});

describe("ingestStageLabel", () => {
  it("formats stage names and handles failed books", () => {
    expect(ingestStageLabel({ status: "failed", stage: "analyze" })).toBe("Import failed");
    expect(ingestStageLabel({ status: "importing", stage: "shot_plan" })).toBe("Shot plan");
    expect(ingestStageLabel({ status: "importing", stage: null })).toBe("Preparing");
  });
});

describe("formatIngestPercent", () => {
  it("clamps and rounds progress to a percent string", () => {
    expect(formatIngestPercent(0.42)).toBe("42%");
    expect(formatIngestPercent(1.2)).toBe("100%");
    expect(formatIngestPercent(null)).toBeNull();
  });
});

describe("LibraryEventSource", () => {
  it("parses ingest_progress SSE payloads", async () => {
    const events: unknown[] = [];
    const holder: { es: EventSourceLike | null } = { es: null };
    const source = new LibraryEventSource({
      baseUrl: "http://api.test",
      getToken: async () => "tok",
      createEventSource: (url) => {
        expect(url).toBe("http://api.test/api/books/events?token=tok");
        const handle: EventSourceLike = {
          close: () => undefined,
          onopen: null,
          onerror: null,
          onmessage: null,
        };
        holder.es = handle;
        return handle;
      },
      onEvent: (event) => events.push(event),
      reconnect: false,
    });
    await source.connect();
    if (!holder.es) throw new Error("expected event source");
    holder.es.onmessage?.({
      data: JSON.stringify({
        event: "ingest_progress",
        book_id: "book_x",
        stage: "analyze",
        pct: 0.42,
      }),
    });
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      event: "ingest_progress",
      book_id: "book_x",
      stage: "analyze",
      pct: 0.42,
    });
    source.close();
  });
});
