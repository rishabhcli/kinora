import { describe, expect, it, vi } from "vitest";

import {
  LibraryEventsClient,
  patchBooksWithIngestProgress,
  statusFromIngestStage,
  type EventSourceLike,
} from "./libraryEvents";
import type { BookResponse } from "../api/types";

function fakeEventSource() {
  const listeners = new Map<string, Set<(event: { data: string }) => void>>();
  let source: EventSourceLike | null = null;
  const factory = (_url: string): EventSourceLike => {
    source = {
      close: vi.fn(),
      onopen: null,
      onerror: null,
      addEventListener(type, listener) {
        const set = listeners.get(type) ?? new Set();
        set.add(listener);
        listeners.set(type, set);
      },
      removeEventListener(type, listener) {
        listeners.get(type)?.delete(listener);
      },
    };
    return source;
  };
  const emit = (type: string, data: string) => {
    for (const listener of listeners.get(type) ?? []) listener({ data });
  };
  const open = () => source?.onopen?.({});
  return { factory, emit, open, get source() {
    return source;
  } };
}

describe("patchBooksWithIngestProgress", () => {
  const books: BookResponse[] = [
    { id: "a", title: "A", status: "importing", progress: 0.1, stage: "extract" },
    { id: "b", title: "B", status: "ready" },
  ];

  it("updates stage and pct for the matching book", () => {
    const next = patchBooksWithIngestProgress(books, {
      event: "ingest_progress",
      book_id: "a",
      stage: "analyze",
      pct: 0.45,
    });
    expect(next[0]?.stage).toBe("analyze");
    expect(next[0]?.progress).toBe(0.45);
    expect(next[0]?.status).toBe("importing");
    expect(next[1]).toEqual(books[1]);
  });

  it("marks ready when the terminal stage arrives", () => {
    const next = patchBooksWithIngestProgress(books, {
      event: "ingest_progress",
      book_id: "a",
      stage: "ready",
      pct: 1,
    });
    expect(next[0]?.status).toBe("ready");
  });
});

describe("statusFromIngestStage", () => {
  it("maps terminal stages", () => {
    expect(statusFromIngestStage("ready")).toBe("ready");
    expect(statusFromIngestStage("failed")).toBe("failed");
    expect(statusFromIngestStage("analyze")).toBeNull();
  });
});

describe("LibraryEventsClient", () => {
  it("parses ingest_progress SSE events", async () => {
    const { factory, emit, open } = fakeEventSource();
    const onProgress = vi.fn();
    const client = new LibraryEventsClient({
      baseUrl: "http://api.test",
      getToken: async () => "tok",
      createEventSource: factory,
      onProgress,
      reconnect: false,
    });
    await client.connect();
    open();
    emit(
      "ingest_progress",
      JSON.stringify({ event: "ingest_progress", book_id: "x", stage: "canon", pct: 0.6 }),
    );
    expect(onProgress).toHaveBeenCalledWith({
      event: "ingest_progress",
      book_id: "x",
      stage: "canon",
      pct: 0.6,
    });
    client.close();
  });
});
