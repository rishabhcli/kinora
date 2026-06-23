/**
 * LibraryEventStream — SSE subscription to `GET /api/books/events` (§5.1).
 *
 * Fans live `ingest_progress` events to the shelf while Phase A runs. Uses the
 * same token-query auth as session SSE (EventSource cannot set headers).
 */
import { type TokenProvider } from "../api/client";
import { parseSessionEvent, type KinoraEvent } from "../events";

export type LibraryStreamStatus = "connecting" | "open" | "closed";

export interface LibraryEventStreamOptions {
  baseUrl: string;
  getToken: TokenProvider;
  /** `(url) => new EventSource(url)` — injected so core stays DOM/RN-free. */
  createEventSource: (url: string) => EventSourceLike;
  onEvent: (event: KinoraEvent) => void;
  onStatus?: (status: LibraryStreamStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

/** Minimal EventSource surface for tests and RN polyfills. */
export interface EventSourceLike {
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: string; type?: string }) => void) | null;
  addEventListener?: (type: string, listener: (event: { data: string }) => void) => void;
  removeEventListener?: (type: string, listener: (event: { data: string }) => void) => void;
}

const MAX_RECONNECT_MS = 10_000;

function eventsUrl(baseUrl: string, token?: string | null): string {
  const root = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${root}/api/books/events${query}`;
}

export class LibraryEventStream {
  private source: EventSourceLike | null = null;
  private closedByUser = false;
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private ingestListener: ((event: { data: string }) => void) | null = null;

  constructor(private readonly opts: LibraryEventStreamOptions) {}

  async connect(): Promise<void> {
    this.closedByUser = false;
    this.opts.onStatus?.("connecting");
    const token = await this.opts.getToken();
    const source = this.opts.createEventSource(eventsUrl(this.opts.baseUrl, token));
    this.source = source;

    const onIngest = (event: { data: string }) => this.handleData(event.data);
    this.ingestListener = onIngest;
    if (source.addEventListener) {
      source.addEventListener("ingest_progress", onIngest);
    }

    source.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    source.onmessage = (event) => {
      if (event.type && event.type !== "message") return;
      this.handleData(event.data);
    };
    source.onerror = () => {
      this.opts.onStatus?.("closed");
      this.detach(source);
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.source) this.detach(this.source);
    this.source = null;
  }

  private detach(source: EventSourceLike): void {
    if (this.ingestListener && source.removeEventListener) {
      source.removeEventListener("ingest_progress", this.ingestListener);
    }
    this.ingestListener = null;
    source.close();
  }

  private handleData(raw: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
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
