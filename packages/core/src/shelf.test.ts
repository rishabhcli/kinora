import { describe, expect, it, vi } from "vitest";

import {
  applyIngestProgress,
  displayBookTitle,
  hasImportingBooks,
  importGateMessage,
  importStageLabel,
  isBookReady,
  LibraryEventStream,
  type EventSourceLike,
} from "./shelf";

const sampleBook = {
  id: "book-1",
  title: "The Frog-King",
  author: "Brothers Grimm",
  status: "importing",
  stage: "analyze",
  progress: 0.45,
  num_pages: null,
  art_direction: null,
  created_at: null,
};

describe("shelf helpers", () => {
  it("labels ingest stages in plain language", () => {
    expect(importStageLabel({ status: "importing", stage: "canon" })).toBe("Building the story canon…");
    expect(importStageLabel({ status: "failed", stage: "analyze" })).toBe("Import failed");
  });

  it("builds an import gate message with progress", () => {
    const msg = importGateMessage(sampleBook);
    expect(msg).toContain("The Frog-King");
    expect(msg).toContain("Understanding the story");
    expect(msg).toContain("45%");
  });

  it("strips e2e seed suffixes from display titles", () => {
    expect(displayBookTitle("Demo (e2e seed)")).toBe("Demo");
  });

  it("detects ready and importing books", () => {
    expect(isBookReady({ status: "ready" })).toBe(true);
    expect(hasImportingBooks([{ status: "ready" }, { status: "importing" }])).toBe(true);
  });

  it("patches a book from ingest_progress", () => {
    const next = applyIngestProgress(sampleBook, {
      book_id: "book-1",
      stage: "shot_plan",
      pct: 0.8,
    });
    expect(next.stage).toBe("shot_plan");
    expect(next.progress).toBe(0.8);
    expect(next.status).toBe("importing");
  });

  it("marks ready when stage is ready", () => {
    const next = applyIngestProgress(sampleBook, { book_id: "book-1", stage: "ready", pct: 1 });
    expect(next.status).toBe("ready");
  });
});

describe("LibraryEventStream", () => {
  it("parses ingest_progress SSE payloads", async () => {
    const events: unknown[] = [];
    let listener: ((event: { data: string }) => void) | undefined;
    const fake: EventSourceLike = {
      close: vi.fn(),
      onopen: null,
      onerror: null,
      onmessage: null,
      addEventListener(type, fn) {
        if (type === "ingest_progress") listener = fn;
      },
    };
    const stream = new LibraryEventStream({
      baseUrl: "http://localhost:8000",
      getToken: async () => "tok",
      createEventSource: () => fake,
      onEvent: (e) => events.push(e),
    });
    await stream.connect();
    if (!listener) throw new Error("listener not registered");
    listener({
      data: JSON.stringify({
        event: "ingest_progress",
        book_id: "b1",
        stage: "analyze",
        pct: 0.2,
      }),
    });
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ event: "ingest_progress", book_id: "b1" });
    stream.close();
  });
});
