/**
 * LibraryEventsSource — SSE client for `GET /api/books/events` (§5.1).
 *
 * EventSource cannot set Authorization headers, so the bearer token is passed
 * as a `?token=` query parameter (same pattern as the session SSE stream).
 * Incoming `ingest_progress` events are validated and forwarded to `onEvent`.
 */
import { type TokenProvider } from "../api/client";
import { ingestProgressEvent, type KinoraEvent } from "../events";

/** Structural subset of `EventSource` — satisfied by DOM and polyfills alike. */
export interface EventSourceLike {
  close(): void;
  // DOM EventSource uses typed handlers; we only assign callbacks.
  onopen: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  addEventListener(type: string, listener: (event: { data: string }) => void): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;
export type LibraryEventsStatus = "connecting" | "open" | "closed";

export interface LibraryEventsOptions {
  baseUrl: string;
  getToken: TokenProvider;
  createEventSource: EventSourceFactory;
  onEvent: (event: KinoraEvent) => void;
  onStatus?: (status: LibraryEventsStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

const MAX_RECONNECT_MS = 10_000;

function libraryEventsUrl(baseUrl: string, token?: string | null): string {
  const root = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${root}/api/books/events${query}`;
}

function parseNamedEvent(data: string, eventName: string): KinoraEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return null;
  }
  if (eventName === "ingest_progress") {
    const result = ingestProgressEvent.safeParse(parsed);
    return result.success ? result.data : null;
  }
  return null;
}

export class LibraryEventsSource {
  private source: EventSourceLike | null = null;
  private closedByUser = false;
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly opts: LibraryEventsOptions) {}

  async connect(): Promise<void> {
    this.closedByUser = false;
    this.opts.onStatus?.("connecting");
    const token = await this.opts.getToken();
    const source = this.opts.createEventSource(libraryEventsUrl(this.opts.baseUrl, token));
    this.source = source;

    source.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    source.addEventListener("ingest_progress", (event) => {
      const parsed = parseNamedEvent(event.data, "ingest_progress");
      if (parsed) this.opts.onEvent(parsed);
    });
    source.onerror = () => {
      this.opts.onStatus?.("closed");
      this.source?.close();
      this.source = null;
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.source?.close();
    this.source = null;
  }

  private scheduleReconnect(): void {
    const base = this.opts.reconnectBaseMs ?? 500;
    const delay = Math.min(base * 2 ** this.attempts, MAX_RECONNECT_MS);
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => {
      void this.connect();
    }, delay);
  }
}
