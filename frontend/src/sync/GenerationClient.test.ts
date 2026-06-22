import { afterEach, describe, expect, it, vi } from "vitest";

import { GenerationClient, parseEvent } from "./GenerationClient";

type Listener = (ev: { data: string }) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: ((ev?: unknown) => void) | null = null;
  onerror: ((ev?: unknown) => void) | null = null;
  onmessage: Listener | null = null;
  listeners: Record<string, Listener[]> = {};
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, cb: Listener): void {
    (this.listeners[type] ??= []).push(cb);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: string): void {
    (this.listeners[type] ?? []).forEach((cb) => cb({ data }));
  }

  message(data: string): void {
    this.onmessage?.({ data });
  }
}

afterEach(() => {
  FakeEventSource.instances = [];
});

describe("parseEvent", () => {
  it("parses a known typed event", () => {
    expect(parseEvent("clip_ready", JSON.stringify({ shot_id: "s1", oss_url: "u" }))).toEqual({
      type: "clip_ready",
      data: { shot_id: "s1", oss_url: "u" },
    });
  });

  it("rejects unknown types and malformed JSON", () => {
    expect(parseEvent("nope", "{}")).toBeNull();
    expect(parseEvent("clip_ready", "{bad json")).toBeNull();
  });
});

describe("GenerationClient — SSE dispatch (mocked EventSource)", () => {
  function setup() {
    const onEvent = vi.fn();
    const onStatus = vi.fn();
    const client = new GenerationClient({
      sessionId: "sess",
      eventsUrl: "/api/sessions/sess/events?token=t",
      onEvent,
      onStatus,
      EventSourceImpl: FakeEventSource as unknown as typeof EventSource,
    });
    client.connect();
    const es = FakeEventSource.instances[0];
    return { client, onEvent, onStatus, es };
  }

  it("opens the stream and reports connection status", () => {
    const { onStatus, es } = setup();
    expect(es.url).toContain("token=t");
    expect(onStatus).toHaveBeenCalledWith("connecting");
    es.onopen?.();
    expect(onStatus).toHaveBeenCalledWith("open");
  });

  it("dispatches every §5.6 event as a typed KinoraEvent", () => {
    const { onEvent, es } = setup();
    es.emit(
      "clip_ready",
      JSON.stringify({ shot_id: "s42", oss_url: "oss://c", sync_segment: { shot_id: "s42" } }),
    );
    es.emit("keyframe_ready", JSON.stringify({ beat_id: "b1", oss_url: "k" }));
    es.emit("budget_low", JSON.stringify({ remaining_s: 120 }));
    es.emit("agent_activity", JSON.stringify({ agent: "Critic", message: "CCS 0.91 pass" }));
    es.emit("conflict_choice", JSON.stringify({ conflict_id: "cf1", options: [] }));

    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ type: "clip_ready" }));
    expect(onEvent).toHaveBeenCalledWith({ type: "budget_low", data: { remaining_s: 120 } });
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ type: "agent_activity" }));
    expect(onEvent).toHaveBeenCalledTimes(5);
  });

  it("falls back to the default-message envelope {type,data}", () => {
    const { onEvent, es } = setup();
    es.message(JSON.stringify({ type: "regen_done", data: { shot_id: "s1", oss_url: "u", qa: {} } }));
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ type: "regen_done" }),
    );
  });

  it("ignores unknown event names", () => {
    const { onEvent, es } = setup();
    es.emit("nonsense", JSON.stringify({ a: 1 }));
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("closes cleanly and reports closed", () => {
    const { client, es, onStatus } = setup();
    client.close();
    expect(es.closed).toBe(true);
    expect(onStatus).toHaveBeenCalledWith("closed");
  });
});
