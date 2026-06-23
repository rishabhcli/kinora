import { describe, expect, it, vi } from "vitest";

import { LibraryEventStream, type EventSourceLike } from "./library";

function fakeSource(): EventSourceLike & { listeners: Map<string, (event: { data: string }) => void> } {
  const listeners = new Map<string, (event: { data: string }) => void>();
  return {
    listeners,
    onopen: null,
    onerror: null,
    close: vi.fn(),
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    removeEventListener(type) {
      listeners.delete(type);
    },
  };
}

describe("LibraryEventStream", () => {
  it("forwards ingest_progress events", async () => {
    const source = fakeSource();
    const onIngestProgress = vi.fn();
    const stream = new LibraryEventStream({
      baseUrl: "http://localhost:8000",
      getToken: async () => "tok",
      onIngestProgress,
      createEventSource: () => source,
    });

    await stream.connect();
    source.listeners.get("ingest_progress")?.({
      data: JSON.stringify({
        event: "ingest_progress",
        book_id: "b1",
        stage: "analyze",
        pct: 0.45,
      }),
    });

    expect(onIngestProgress).toHaveBeenCalledWith(
      expect.objectContaining({ book_id: "b1", stage: "analyze", pct: 0.45 }),
    );
    stream.close();
  });
});
