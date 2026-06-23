/**
 * LibraryEventStream — SSE client for `GET /api/books/events` (§5.1).
 *
 * The shelf subscribes here for live `ingest_progress` events while Phase A
 * runs. EventSource cannot set Authorization headers, so the backend accepts
 * `?token=` (same pattern as session SSE). The factory is injected so core
 * stays free of DOM/RN dependencies and is unit-testable.
 */
import { type TokenProvider } from "../api/client";
import { type BookResponse } from "../api/types";
import { ingestProgressEvent } from "../events";

/** Structural subset of `EventSource` — satisfied by the browser API. */
export interface EventSourceLike {
  close(): void;
  addEventListener(type: string, listener: (event: { data: string }) => void): void;
  onopen: unknown;
  onerror: unknown;
}

export type EventSourceFactory = (url: string) => EventSourceLike;
export type LibraryStreamStatus = "connecting" | "open" | "closed";

export type IngestProgressPayload = {
  event: "ingest_progress";
  book_id: string;
  stage?: string;
  pct?: number;
  status?: string;
};

export interface LibraryEventStreamOptions {
  baseUrl: string;
  getToken: TokenProvider;
  createEventSource: EventSourceFactory;
  onIngestProgress: (event: IngestProgressPayload) => void;
  onStatus?: (status: LibraryStreamStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

/** Poll interval while any book on the shelf is still importing. */
export const INGEST_POLL_MS = 3_000;

const MAX_RECONNECT_MS = 10_000;

function eventsUrl(baseUrl: string, token?: string | null): string {
  const base = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${base}/api/books/events${query}`;
}

/** True when the shelf should keep polling `GET /api/books` for status flips. */
export function shelfHasImporting(books: BookResponse[] | undefined): boolean {
  return (books ?? []).some((b) => b.status === "importing");
}

/** Merge a live ingest_progress event into a React Query books list cache. */
export function applyIngestProgress(
  books: BookResponse[] | undefined,
  raw: unknown,
): BookResponse[] | undefined {
  if (!books) return books;
  const parsed = ingestProgressEvent.safeParse(raw);
  if (!parsed.success) return books;
  const event = parsed.data;
  const idx = books.findIndex((b) => b.id === event.book_id);
  if (idx < 0) return books;
  const book = books[idx]!;
  const pct = typeof event.pct === "number" ? event.pct : book.progress;
  const stage = typeof event.stage === "string" ? event.stage : book.stage;
  const status = typeof event.status === "string" ? event.status : book.status;
  const next = [...books];
  next[idx] = { ...book, stage, progress: pct, status };
  return next;
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
    const source = this.opts.createEventSource(eventsUrl(this.opts.baseUrl, token));
    this.source = source;

    source.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    source.onerror = () => {
      this.opts.onStatus?.("closed");
      this.source = null;
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
    source.addEventListener("ingest_progress", (event) => {
      this.handlePayload(event.data);
    });
    // Some transports only surface unnamed `message` events.
    source.addEventListener("message", (event) => {
      this.handlePayload(event.data);
    });
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.source?.close();
    this.source = null;
    this.opts.onStatus?.("closed");
  }

  private handlePayload(data: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      return;
    }
    const result = ingestProgressEvent.safeParse(parsed);
    if (!result.success) return;
    this.opts.onIngestProgress(result.data as IngestProgressPayload);
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
