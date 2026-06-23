/**
 * LibraryEventSource — the shelf's ingest-progress channel over
 * `GET /api/books/events` (§5.1). EventSource is used (not WebSocket) because
 * the backend exposes a one-way SSE stream and both shells have a native
 * `EventSource`; the constructor is injected so core stays testable.
 */
import { type TokenProvider } from "../api/client";
import type { BookResponse } from "../api/types";
import { parseSessionEvent, type KinoraEvent } from "../events";

/** The structural subset of `EventSource` we use — satisfied by DOM and polyfills. */
export interface EventSourceLike {
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
}

export type EventSourceFactory = (url: string) => EventSourceLike;
export type LibrarySourceStatus = "connecting" | "open" | "closed";

export interface IngestProgressUpdate {
  book_id: string;
  stage?: string;
  pct?: number;
}

export interface LibraryEventSourceOptions {
  /** Backend base URL, e.g. `http://localhost:8000`. */
  baseUrl: string;
  getToken: TokenProvider;
  /** `(url) => new EventSource(url)` — injected so core needs no DOM lib. */
  createEventSource: EventSourceFactory;
  onEvent: (event: KinoraEvent) => void;
  onStatus?: (status: LibrarySourceStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

const MAX_RECONNECT_MS = 10_000;

function libraryEventsUrl(baseUrl: string, token?: string | null): string {
  const root = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${root}/api/books/events${query}`;
}

/** Merge a live `ingest_progress` event into a cached `GET /books` list. */
export function applyIngestProgress(
  books: BookResponse[],
  update: IngestProgressUpdate,
): BookResponse[] {
  const stage = update.stage?.trim();
  let status: string | undefined;
  if (stage === "ready") status = "ready";
  else if (stage === "failed") status = "failed";
  else if (stage) status = "importing";

  return books.map((book) => {
    if (book.id !== update.book_id) return book;
    return {
      ...book,
      ...(status ? { status } : {}),
      ...(stage ? { stage } : {}),
      ...(update.pct !== undefined ? { progress: update.pct } : {}),
    };
  });
}

/** A short, human label for a book that is still importing (or failed). */
export function ingestStageLabel(book: Pick<BookResponse, "status" | "stage">): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage === "ready") return "Ready";
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

/** Format ingest progress (0..1) for display, e.g. `42%`. */
export function formatIngestPercent(progress: number | null | undefined): string | null {
  if (progress === null || progress === undefined || Number.isNaN(progress)) return null;
  const pct = Math.round(Math.min(1, Math.max(0, progress)) * 100);
  return `${pct}%`;
}

export class LibraryEventSource {
  private es: EventSourceLike | null = null;
  private closedByUser = false;
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly opts: LibraryEventSourceOptions) {}

  async connect(): Promise<void> {
    this.closedByUser = false;
    this.opts.onStatus?.("connecting");
    const token = await this.opts.getToken();
    const es = this.opts.createEventSource(libraryEventsUrl(this.opts.baseUrl, token));
    this.es = es;

    es.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    es.onmessage = (event) => this.handleMessage(event.data);
    es.onerror = () => {
      this.opts.onStatus?.("closed");
      this.es = null;
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.es?.close();
    this.es = null;
    this.opts.onStatus?.("closed");
  }

  private handleMessage(data: string): void {
    if (!data) return;
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

/** Convenience: subscribe to ingest progress and return a disposer. */
export function subscribeLibraryIngest(opts: {
  baseUrl: string;
  getToken: TokenProvider;
  createEventSource: EventSourceFactory;
  onProgress: (update: IngestProgressUpdate) => void;
}): () => void {
  const source = new LibraryEventSource({
    baseUrl: opts.baseUrl,
    getToken: opts.getToken,
    createEventSource: opts.createEventSource,
    onEvent: (event) => {
      if (event.event !== "ingest_progress") return;
      const raw = event as IngestProgressUpdate & { event: "ingest_progress" };
      opts.onProgress({
        book_id: raw.book_id,
        stage: typeof raw.stage === "string" ? raw.stage : undefined,
        pct: typeof raw.pct === "number" ? raw.pct : undefined,
      });
    },
    reconnect: true,
  });
  void source.connect();
  return () => source.close();
}
