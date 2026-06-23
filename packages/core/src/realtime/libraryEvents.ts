/**
 * LibraryEventsClient — live ingest progress on the shelf via
 * `GET /api/books/events` (SSE). EventSource cannot set Authorization headers,
 * so the token travels in `?token=` like the session SSE route. The client is
 * injected (DOM EventSource on desktop; mobile falls back to polling only).
 */
import { type TokenProvider } from "../api/client";
import type { BookResponse } from "../api/types";

/** Raw ingest_progress payload from the backend Redis pub/sub channel. */
export interface IngestProgressPayload {
  event: "ingest_progress";
  book_id: string;
  stage?: string;
  pct?: number;
}

/** Minimal EventSource surface — satisfied by the DOM API and test fakes. */
export interface EventSourceLike {
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  addEventListener(type: string, listener: (event: { data: string }) => void): void;
  removeEventListener(type: string, listener: (event: { data: string }) => void): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;
export type LibraryEventsStatus = "connecting" | "open" | "closed";

export interface LibraryEventsOptions {
  baseUrl: string;
  getToken: TokenProvider;
  createEventSource?: EventSourceFactory;
  onProgress: (payload: IngestProgressPayload) => void;
  onStatus?: (status: LibraryEventsStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

const MAX_RECONNECT_MS = 10_000;

function sseUrl(baseUrl: string, token?: string | null): string {
  const base = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${base}/api/books/events${query}`;
}

/** Map an ingest_progress stage to the shelf book status when pct is terminal. */
export function statusFromIngestStage(stage: string | undefined): BookResponse["status"] | null {
  if (stage === "ready") return "ready";
  if (stage === "failed") return "failed";
  return null;
}

/** Patch the cached books list with a live ingest_progress event. */
export function patchBooksWithIngestProgress(
  books: BookResponse[],
  progress: IngestProgressPayload,
): BookResponse[] {
  const nextStatus = statusFromIngestStage(progress.stage);
  return books.map((book) => {
    if (book.id !== progress.book_id) return book;
    return {
      ...book,
      stage: progress.stage ?? book.stage,
      progress: progress.pct ?? book.progress,
      status: nextStatus ?? book.status,
    };
  });
}

export class LibraryEventsClient {
  private source: EventSourceLike | null = null;
  private closedByUser = false;
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private ingestListener: ((event: { data: string }) => void) | null = null;

  constructor(private readonly opts: LibraryEventsOptions) {}

  async connect(): Promise<void> {
    if (!this.opts.createEventSource) {
      this.opts.onStatus?.("closed");
      return;
    }
    this.closedByUser = false;
    this.opts.onStatus?.("connecting");
    const token = await this.opts.getToken();
    const source = this.opts.createEventSource(sseUrl(this.opts.baseUrl, token));
    this.source = source;

    this.ingestListener = (event) => this.handleIngest(event.data);
    source.addEventListener("ingest_progress", this.ingestListener);

    source.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    source.onerror = () => {
      this.opts.onStatus?.("closed");
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.source && this.ingestListener) {
      this.source.removeEventListener("ingest_progress", this.ingestListener);
    }
    this.source?.close();
    this.source = null;
    this.ingestListener = null;
  }

  private handleIngest(raw: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      (parsed as { event?: string }).event !== "ingest_progress" ||
      typeof (parsed as { book_id?: unknown }).book_id !== "string"
    ) {
      return;
    }
    this.opts.onProgress(parsed as IngestProgressPayload);
  }

  private scheduleReconnect(): void {
    this.close();
    this.closedByUser = false;
    const base = this.opts.reconnectBaseMs ?? 500;
    const delay = Math.min(base * 2 ** this.attempts, MAX_RECONNECT_MS);
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => {
      void this.connect();
    }, delay);
  }
}

/** True when any book on the shelf is still being adapted. */
export function shelfHasPendingImports(books: BookResponse[] | undefined): boolean {
  return (books ?? []).some((book) => book.status === "importing");
}
