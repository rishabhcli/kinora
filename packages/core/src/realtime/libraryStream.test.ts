import { describe, expect, it, vi } from "vitest";

import { LibraryEventStream, type EventSourceLike } from "./libraryStream";

function fakeEventSource(): EventSourceLike & { url: string; listeners: Map<string, (e: { data: string }) => void> } {
  const listeners = new Map<string, (e: { data: string }) => void>();
  return {
    url: "",
    listeners,
    close: vi.fn(),
    onopen: null,
    onerror: null,
    onmessage: null,
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    removeEventListener(type) {
      listeners.delete(type);
    },
  };
}

describe("LibraryEventStream", () => {
  it("parses ingest_progress events", async () => {
    let captured: unknown = null;
    const sources: ReturnType<typeof fakeEventSource>[] = [];
    const stream = new LibraryEventStream({
      baseUrl: "http://localhost:8000",
      getToken: async () => "tok",
      createEventSource: (url) => {
        const es = fakeEventSource();
        es.url = url;
        sources.push(es);
        queueMicrotask(() => es.onopen?.(null));
        return es;
      },
      onEvent: (event) => {
        captured = event;
      },
      reconnect: false,
    });

    await stream.connect();
    expect(sources[0]?.url).toContain("token=tok");
    sources[0]?.listeners.get("ingest_progress")?.({
      data: JSON.stringify({ event: "ingest_progress", book_id: "b1", stage: "analyse", pct: 0.2 }),
    });
    expect(captured).toMatchObject({ event: "ingest_progress", book_id: "b1", stage: "analyse", pct: 0.2 });
    stream.close();
  });
});
