/**
 * Typed Server-Sent-Events streaming for the Kinora SDK.
 *
 * The backend streams session/library events as SSE frames
 * (`event: <name>\ndata: <json>\n\n`, plus `:`-prefixed comment keepalives).
 * Rather than the browser-only `EventSource`, we parse the `fetch` response
 * body's `ReadableStream`, so streaming works in Node 20+ too and the bearer
 * token rides in the `Authorization` header (no `?token=` leak required —
 * though the SDK still supports it for parity with the renderer).
 *
 * `parseSseStream` is a pure async generator over a byte stream → typed events.
 */

import type { FilmSyncMap, Json } from "./models.js";

// --------------------------------------------------------------------------- //
// Typed event payloads (mirror clients/spec EVENTS + backend publishers)
// --------------------------------------------------------------------------- //

export interface BufferStateEvent {
  event: "buffer_state";
  committed_seconds_ahead: number;
  bursting: boolean;
  idle: boolean;
  velocity_wps?: number;
  budget_remaining_s: number | null;
}

export interface ClipReadyEvent {
  event: "clip_ready";
  shot_id: string;
  oss_url: string;
  video_seconds?: number;
}

export interface KeyframeReadyEvent {
  event: "keyframe_ready";
  shot_id: string;
  beat_id?: string;
  oss_url: string;
}

export interface SceneStitchedEvent {
  event: "scene_stitched";
  scene_id: string;
  oss_url: string;
  sync_map: FilmSyncMap;
}

export interface EventStitchedEvent {
  event: "event_stitched";
  event_id: string;
  oss_url: string;
  sync_map: FilmSyncMap;
}

export interface AgentActivityEvent {
  event: "agent_activity";
  agent: string;
  aspect?: string;
  message: string;
  shot_id?: string | null;
  job_id?: string | null;
  conflict?: Json | null;
}

export interface RegenDoneEvent {
  event: "regen_done";
  shot_id: string;
  oss_url: string | null;
  qa?: Json | null;
}

export interface BudgetLowEvent {
  event: "budget_low";
  budget_remaining_s: number;
  scope?: string;
}

export interface ConflictChoiceEvent {
  event: "conflict_choice";
  conflict_id: string;
  options: Json[];
  claim?: string;
  canon_fact?: string;
  current_beat?: string | null;
  raised_by?: string;
  shot_id?: string;
}

export interface IngestProgressEvent {
  event: "ingest_progress";
  book_id?: string;
  stage: string;
  pct: number;
}

/**
 * An event whose `event` name the SDK does not (yet) model — still delivered.
 *
 * Kept OUT of the {@link KnownEvent} discriminated union (a non-literal
 * discriminant would break narrowing on the known members). It is unioned in at
 * {@link SessionEvent}, so `switch (ev.event)` narrows each known case to its
 * exact typed payload and an unmodelled name (a newer backend's event) still
 * flows through as an `UnknownEvent` in the `default`/`else`.
 */
export interface UnknownEvent {
  event: string;
  [field: string]: unknown;
}

/** The discriminated union of every *modelled* session/book event. */
export type KnownEvent =
  | BufferStateEvent
  | ClipReadyEvent
  | KeyframeReadyEvent
  | SceneStitchedEvent
  | EventStitchedEvent
  | AgentActivityEvent
  | RegenDoneEvent
  | BudgetLowEvent
  | ConflictChoiceEvent
  | IngestProgressEvent;

/**
 * Every session/book event: the modelled ones (which narrow on `event`) plus an
 * open `UnknownEvent` fallback for names a newer backend may add.
 */
export type SessionEvent = KnownEvent | UnknownEvent;

/** Library-channel events (ingest progress only, today). */
export type LibraryEvent = IngestProgressEvent | UnknownEvent;

/** Map a known event name to its typed payload. */
export type EventByName = {
  [E in KnownEvent as E["event"]]: E;
};

/** The set of names the SDK models with a typed payload. */
export type KnownEventName = keyof EventByName;

/**
 * A type guard that narrows a {@link SessionEvent} to a specific known event.
 *
 * Because `UnknownEvent.event` is an open `string`, a bare `ev.event === "x"`
 * check cannot fully exclude it from the union (TypeScript keeps `UnknownEvent`
 * in scope, so field access widens to `unknown`). This guard narrows cleanly:
 *
 * ```ts
 * if (isEvent(ev, "clip_ready")) play(ev.oss_url); // ev: ClipReadyEvent
 * ```
 */
export function isEvent<N extends KnownEventName>(
  ev: SessionEvent,
  name: N,
): ev is EventByName[N] {
  return ev.event === name;
}

/** A raw SSE frame before JSON parsing. */
export interface RawSseFrame {
  event: string;
  data: string;
  id?: string;
}

// --------------------------------------------------------------------------- //
// SSE byte-stream parser
// --------------------------------------------------------------------------- //

/** Split accumulated text into complete SSE frames; returns [frames, remainder]. */
function splitFrames(buffer: string): [string[], string] {
  const frames: string[] = [];
  let rest = buffer;
  // SSE frames are separated by a blank line. Tolerate \n\n and \r\n\r\n.
  let idx: number;
  // eslint-disable-next-line no-cond-assign
  while ((idx = indexOfFrameBoundary(rest)) !== -1) {
    const boundaryLen = rest.startsWith("\r\n\r\n", idx) ? 4 : 2;
    frames.push(rest.slice(0, idx));
    rest = rest.slice(idx + boundaryLen);
  }
  return [frames, rest];
}

function indexOfFrameBoundary(s: string): number {
  const a = s.indexOf("\n\n");
  const b = s.indexOf("\r\n\r\n");
  if (a === -1) return b;
  if (b === -1) return a;
  return Math.min(a, b);
}

/** Parse one SSE frame block into its fields. Returns null for pure comments. */
export function parseFrame(block: string): RawSseFrame | null {
  let event = "message";
  const dataLines: string[] = [];
  let id: string | undefined;
  let sawField = false;
  for (const lineRaw of block.split(/\r?\n/)) {
    const line = lineRaw;
    if (line.startsWith(":")) continue; // comment / keepalive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") {
      event = value;
      sawField = true;
    } else if (field === "data") {
      dataLines.push(value);
      sawField = true;
    } else if (field === "id") {
      id = value;
      sawField = true;
    }
  }
  if (!sawField || dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n"), id };
}

/**
 * Async generator yielding typed events from a byte stream (the `fetch`
 * response body). The `event` field on each yielded object is the SSE event
 * name (falling back to the JSON payload's own `event` if the SSE name is the
 * default `message`), so the discriminated union always carries a reliable tag.
 */
export async function* parseSseStream<T extends { event: string } = SessionEvent>(
  stream: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<T> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      if (signal?.aborted) return;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const [blocks, rest] = splitFrames(buffer);
      buffer = rest;
      for (const block of blocks) {
        const frame = parseFrame(block);
        if (!frame) continue;
        const typed = toTypedEvent<T>(frame);
        if (typed) yield typed;
      }
    }
    // Flush any trailing complete frame without a final blank line.
    const tail = parseFrame(buffer);
    if (tail) {
      const typed = toTypedEvent<T>(tail);
      if (typed) yield typed;
    }
  } finally {
    reader.releaseLock();
  }
}

function toTypedEvent<T extends { event: string }>(frame: RawSseFrame): T | null {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(frame.data) as Record<string, unknown>;
  } catch {
    return null; // non-JSON data (shouldn't happen for real events)
  }
  // Prefer an explicit SSE event name; else trust the payload's own `event`.
  const name = frame.event && frame.event !== "message" ? frame.event : String(payload.event ?? "message");
  return { ...payload, event: name } as T;
}
