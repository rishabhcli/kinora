/**
 * Shelf helpers — human import copy, open gates, and the live library SSE stream.
 *
 * Phase A ingest can take several minutes; the backend publishes `ingest_progress`
 * on `GET /api/books/events`. Desktop shells subscribe via EventSource; mobile
 * polls while any book is still importing (no native EventSource in RN).
 */
import { type TokenProvider } from "./api/client";
import type { components } from "./api/schema";
import { parseSessionEvent, type KinoraEvent } from "./events";

type BookResponse = components["schemas"]["BookResponse"];

/** Structural subset of `EventSource` — satisfied by the DOM and test fakes. */
export interface EventSourceLike {
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  addEventListener(
    type: string,
    listener: (event: { data: string }) => void,
    options?: { once?: boolean },
  ): void;
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

function toLibraryEventsUrl(baseUrl: string, token?: string | null): string {
  const root = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${root}/api/books/events${query}`;
}

/** Live shelf SSE client for `/api/books/events` (ingest_progress). */
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
    const url = toLibraryEventsUrl(this.opts.baseUrl, token);
    const source = this.opts.createEventSource(url);
    this.source = source;

    source.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    source.onerror = () => {
      if (!this.closedByUser && (this.opts.reconnect ?? true)) {
        source.close();
        this.source = null;
        this.opts.onStatus?.("closed");
        this.scheduleReconnect();
      }
    };
    const onPayload = (event: { data: string }) => this.handleData(event.data);
    source.onmessage = onPayload;
    source.addEventListener("ingest_progress", onPayload);
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.source?.close();
    this.source = null;
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

/** Whether the book can be opened in the reading room. */
export function isBookReady(book: Pick<BookResponse, "status">): boolean {
  return book.status === "ready";
}

/** True when any shelf book is still in Phase A. */
export function hasImportingBooks(books: readonly Pick<BookResponse, "status">[]): boolean {
  return books.some((b) => b.status === "importing");
}

/** Strip internal suffixes from titles shown on the shelf. */
export function displayBookTitle(title: string): string {
  return title.replace(/\s*\(e2e seed\)\s*$/i, "").trim();
}

const STAGE_LABELS: Record<string, string> = {
  importing: "Starting import…",
  extract: "Reading your pages…",
  analyze: "Understanding the story…",
  canon: "Building the story canon…",
  shot_plan: "Planning the film…",
  identity_lock: "Locking character looks…",
  ready: "Ready to read",
  failed: "Import failed",
};

/** Human label for an ingest stage chip. */
export function importStageLabel(book: Pick<BookResponse, "status" | "stage">): string {
  if (book.status === "failed") return STAGE_LABELS.failed!;
  const stage = book.stage?.trim().toLowerCase();
  if (stage && STAGE_LABELS[stage]) return STAGE_LABELS[stage]!;
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return book.status === "importing" ? "Preparing your book…" : "Preparing";
}

/** Why a book cannot be opened yet — shown in the import gate dialog. */
export function importGateMessage(book: Pick<BookResponse, "status" | "stage" | "title" | "progress">): string {
  const title = displayBookTitle(book.title);
  if (book.status === "failed") {
    return `“${title}” could not be imported. Try uploading it again, or remove it from your shelf.`;
  }
  const stage = importStageLabel(book);
  const pct = book.progress != null ? Math.round(book.progress * 100) : null;
  const progress = pct != null && pct > 0 && pct < 100 ? ` (${pct}% complete)` : "";
  return `Kinora is still adapting “${title}” — ${stage}${progress}. You can open it as soon as the film is ready.`;
}

/** Patch a book row from a live `ingest_progress` event. */
export function applyIngestProgress(
  book: BookResponse,
  event: { book_id: string; stage?: unknown; pct?: unknown },
): BookResponse {
  if (book.id !== event.book_id) return book;
  const stage = typeof event.stage === "string" ? event.stage : book.stage;
  const pct = typeof event.pct === "number" ? event.pct : book.progress;
  const status =
    stage === "ready" ? "ready" : stage === "failed" ? "failed" : book.status === "ready" ? "ready" : "importing";
  return { ...book, stage, progress: pct, status };
}

/** Poll interval while books are importing (mobile / SSE fallback). */
export const IMPORT_POLL_MS = 5_000;
