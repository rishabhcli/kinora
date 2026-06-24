/**
 * LibraryEventStream — SSE client for `GET /api/books/events` (§5.1).
 *
 * EventSource cannot set Authorization headers, so the backend accepts a
 * `?token=` query parameter (same as session SSE). The constructor injects
 * `createEventSource` so core stays free of DOM/RN dependencies.
 */
import type { TokenProvider } from "../api/client";
import { parseSessionEvent, type KinoraEvent } from "../events";

export interface EventSourceLike {
  close(): void;
  onopen: ((event?: unknown) => void) | null;
  onerror: ((event?: unknown) => void) | null;
  addEventListener(type: string, listener: (event: { data: string }) => void): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;
export type LibraryStreamStatus = "connecting" | "open" | "closed";

export interface LibraryEventStreamOptions {
  baseUrl: string;
  getToken: TokenProvider;
  createEventSource: EventSourceFactory;
  onEvent: (event: KinoraEvent) => void;
  onStatus?: (status: LibraryStreamStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

const MAX_RECONNECT_MS = 10_000;

function libraryEventsUrl(baseUrl: string, token?: string | null): string {
  const root = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${root}/api/books/events${query}`;
}

export class LibraryEventStream {
  private source: EventSourceLike | null = null;
  private closedByUser = false;
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly opts: LibraryEventStreamOptions) {}

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
    source.onerror = () => {
      this.opts.onStatus?.("closed");
      this.source?.close();
      this.source = null;
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
    source.addEventListener("ingest_progress", (event) => this.handleData(event.data));
    source.addEventListener("message", (event) => this.handleData(event.data));
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.source?.close();
    this.source = null;
    this.opts.onStatus?.("closed");
  }

  private handleData(data: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      return;
    }
    const event = parseSessionEvent(parsed);
    if (event) this.opts.onEvent(event);
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
