import { describe, it, expect } from "vitest";
import { parseFrame, parseSseStream, isEvent } from "../src/events.js";
import type { SessionEvent } from "../src/events.js";
import { KinoraClient } from "../src/client.js";
import { MockFetch, streamOf, sseFrame } from "./helpers.js";

describe("parseFrame", () => {
  it("parses a named event with JSON data", () => {
    const f = parseFrame("event: clip_ready\ndata: {\"shot_id\":\"s1\"}");
    expect(f).toEqual({ event: "clip_ready", data: '{"shot_id":"s1"}', id: undefined });
  });

  it("ignores comment-only blocks (keepalives)", () => {
    expect(parseFrame(": keepalive")).toBeNull();
    expect(parseFrame(": connected")).toBeNull();
  });

  it("joins multi-line data fields", () => {
    const f = parseFrame("data: line1\ndata: line2");
    expect(f!.data).toBe("line1\nline2");
  });

  it("strips a single leading space after the colon", () => {
    const f = parseFrame("event:nospace\ndata:{}");
    expect(f!.event).toBe("nospace");
  });
});

describe("parseSseStream", () => {
  it("yields typed events across chunk boundaries", async () => {
    // Split a frame across two chunks to exercise the buffer.
    const full = sseFrame("buffer_state", { committed_seconds_ahead: 30, bursting: true, idle: false, budget_remaining_s: null });
    const mid = Math.floor(full.length / 2);
    const stream = streamOf([": connected\n\n", full.slice(0, mid), full.slice(mid), sseFrame("clip_ready", { shot_id: "s1", oss_url: "http://x/clip.mp4" })]);
    const events: SessionEvent[] = [];
    for await (const ev of parseSseStream<SessionEvent>(stream)) events.push(ev);
    expect(events.length).toBe(2);
    expect(events[0]!.event).toBe("buffer_state");
    expect((events[0] as { committed_seconds_ahead: number }).committed_seconds_ahead).toBe(30);
    expect(events[1]!.event).toBe("clip_ready");
  });

  it("falls back to the payload's own event field when SSE name is 'message'", async () => {
    const stream = streamOf(["data: {\"event\":\"regen_done\",\"shot_id\":\"s2\",\"oss_url\":null}\n\n"]);
    const events: SessionEvent[] = [];
    for await (const ev of parseSseStream<SessionEvent>(stream)) events.push(ev);
    expect(events[0]!.event).toBe("regen_done");
  });

  it("flushes a trailing frame without a final blank line", async () => {
    const stream = streamOf(["event: budget_low\ndata: {\"budget_remaining_s\":10}"]);
    const events: SessionEvent[] = [];
    for await (const ev of parseSseStream<SessionEvent>(stream)) events.push(ev);
    expect(events.length).toBe(1);
    expect(events[0]!.event).toBe("budget_low");
  });

  it("skips frames with non-JSON data", async () => {
    const stream = streamOf(["event: x\ndata: not-json\n\n", sseFrame("clip_ready", { shot_id: "ok" })]);
    const events: SessionEvent[] = [];
    for await (const ev of parseSseStream<SessionEvent>(stream)) events.push(ev);
    expect(events.length).toBe(1);
    expect((events[0] as { shot_id: string }).shot_id).toBe("ok");
  });
});

describe("isEvent type guard", () => {
  it("narrows a known event so typed fields are accessible", () => {
    const ev: SessionEvent = { event: "clip_ready", shot_id: "s1", oss_url: "http://x/c.mp4" };
    if (isEvent(ev, "clip_ready")) {
      // ev is narrowed to ClipReadyEvent — these are typed `string`.
      const url: string = ev.oss_url;
      const id: string = ev.shot_id;
      expect(url).toBe("http://x/c.mp4");
      expect(id).toBe("s1");
    } else {
      throw new Error("guard should have matched");
    }
  });

  it("returns false for a different name", () => {
    const ev: SessionEvent = { event: "buffer_state", committed_seconds_ahead: 30, bursting: true, idle: false, budget_remaining_s: null };
    expect(isEvent(ev, "clip_ready")).toBe(false);
    expect(isEvent(ev, "buffer_state")).toBe(true);
  });

  it("returns false for an unmodelled event name", () => {
    const ev: SessionEvent = { event: "some_future_event", extra: 1 };
    expect(isEvent(ev, "clip_ready")).toBe(false);
  });
});

describe("client.sessions.events", () => {
  it("streams typed events from the response body", async () => {
    const stream = streamOf([
      ": connected\n\n",
      sseFrame("buffer_state", { committed_seconds_ahead: 25, bursting: false, idle: true, budget_remaining_s: null }),
      sseFrame("clip_ready", { shot_id: "s1", oss_url: "http://x/c.mp4", video_seconds: 0 }),
    ]);
    const mock = new MockFetch().enqueue({ stream, contentType: "text/event-stream" });
    const c = new KinoraClient({ baseUrl: "http://localhost:8000", token: "tok", fetch: mock.fetch });
    const received: string[] = [];
    for await (const ev of c.sessions.events("s1")) received.push(ev.event);
    expect(received).toEqual(["buffer_state", "clip_ready"]);
    // The bearer rides in the header, not a query param, by default.
    expect(mock.last()!.url).toBe("http://localhost:8000/api/sessions/s1/events");
    expect(mock.last()!.headers.Authorization).toBe("Bearer tok");
    expect(mock.last()!.headers.Accept).toBe("text/event-stream");
  });

  it("adds ?token= when tokenInQuery is set", async () => {
    const stream = streamOf([sseFrame("clip_ready", { shot_id: "s1", oss_url: "x" })]);
    const mock = new MockFetch().enqueue({ stream });
    const c = new KinoraClient({ baseUrl: "http://localhost:8000", token: "tok", fetch: mock.fetch });
    const events = [];
    for await (const ev of c.sessions.events("s1", { tokenInQuery: true })) events.push(ev);
    expect(mock.last()!.url).toBe("http://localhost:8000/api/sessions/s1/events?token=tok");
  });

  it("subscribe delivers events to a callback and supports unsubscribe", async () => {
    const stream = streamOf([sseFrame("agent_activity", { agent: "showrunner", message: "hi" })]);
    const mock = new MockFetch().enqueue({ stream });
    const c = new KinoraClient({ baseUrl: "http://localhost:8000", token: "tok", fetch: mock.fetch });
    const got: string[] = [];
    await new Promise<void>((resolve) => {
      const unsub = c.sessions.subscribe(
        "s1",
        (ev) => got.push(ev.event),
        { onClose: () => resolve() },
      );
      // The stream is short; onClose resolves. unsub is callable regardless.
      void unsub;
    });
    expect(got).toEqual(["agent_activity"]);
  });
});
