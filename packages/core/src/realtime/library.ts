/**
 * LibraryEventStream — the shelf's one-way SSE channel over
 * ``GET /api/books/events`` (§5.1). EventSource cannot set Authorization, so the
 * bearer token travels in ``?token=`` like the session SSE route. The stream
 * forwards ``ingest_progress`` while Phase A runs; reconnect uses exponential
 * backoff so a flaky tab still catches up via the poll fallback the shell adds.
 */
import type { TokenProvider } from "../api/client";
import { parseSessionEvent } from "../events";

/** The structural subset of DOM EventSource we rely on — injectable for tests. */
export interface EventSourceLike {
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  addEventListener(type: string, listener: (event: { data: string }) => void): void;
  removeEventListener(type: string, listener: (event: { data: string }) => void): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;
export type LibraryStreamStatus = "connecting" | "open" | "closed";

export interface IngestProgressEvent {
  event: "ingest_progress";
  book_id: string;
  stage?: string;
  pct?: number;
}

export interface LibraryEventStreamOptions {
  baseUrl: string;
  getToken: TokenProvider;
  onIngestProgress: (event: IngestProgressEvent) => void;
  onStatus?: (status: LibraryStreamStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
  createEventSource?: EventSourceFactory;
}

const MAX_RECONNECT_MS = 10_000;
const INGEST_EVENT = "ingest_progress";

function defaultEventSourceFactory(): EventSourceFactory {
  return (url) => {
    if (typeof EventSource === "undefined") {
      throw new Error("EventSource is not available in this environment");
    }
    return new EventSource(url) as unknown as EventSourceLike;
  };
}

function libraryEventsUrl(baseUrl: string, token: string | null | undefined): string {
  const root = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${root}/api/books/events${query}`;
}

function parseIngestPayload(raw: string): IngestProgressEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  const event = parseSessionEvent(parsed);
  if (!event || event.event !== INGEST_EVENT) return null;
  return event as IngestProgressEvent;
}

export class LibraryEventStream {
  private source: EventSourceLike | null = null;
  private closedByUser = false;
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly onIngest: (event: { data: string }) => void;
  private readonly createEventSource: EventSourceFactory;

  constructor(private readonly opts: LibraryEventStreamOptions) {
    this.createEventSource = opts.createEventSource ?? defaultEventSourceFactory();
    this.onIngest = (message) => {
      const event = parseIngestPayload(message.data);
      if (event) this.opts.onIngestProgress(event);
    };
  }

  async connect(): Promise<void> {
    this.closedByUser = false;
    this.opts.onStatus?.("connecting");
    const token = await this.opts.getToken();
    const url = libraryEventsUrl(this.opts.baseUrl, token);
    const source = this.createEventSource(url);
    this.source = source;

    source.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    source.onerror = () => {
      this.detach();
      this.opts.onStatus?.("closed");
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
    source.addEventListener(INGEST_EVENT, this.onIngest);
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.detach();
    this.opts.onStatus?.("closed");
  }

  private detach(): void {
    if (!this.source) return;
    this.source.removeEventListener(INGEST_EVENT, this.onIngest);
    this.source.close();
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
