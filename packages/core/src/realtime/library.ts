/**
 * LibraryEvents — the shelf's ingest-progress channel over
 * `GET /api/books/events` (§5.1). Uses fetch + ReadableStream so the same
 * client works in Electron, Expo, and unit tests without an EventSource polyfill.
 *
 * Incoming `ingest_progress` events are validated by {@link parseSessionEvent};
 * {@link patchBooksIngestProgress} patches the React Query books list in place.
 */
import { type TokenProvider } from "../api/client";
import { type BookResponse } from "../api/types";
import { parseSessionEvent, type KinoraEvent } from "../events";

export type LibraryEventsStatus = "connecting" | "open" | "closed";

export interface LibraryEventsOptions {
  /** Backend base URL, e.g. `http://localhost:8000`. */
  baseUrl: string;
  getToken: TokenProvider;
  onEvent: (event: KinoraEvent) => void;
  onStatus?: (status: LibraryEventsStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

const MAX_RECONNECT_MS = 10_000;

function libraryEventsUrl(baseUrl: string, token?: string | null): string {
  const base = baseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${base}/api/books/events${query}`;
}

/** Parse one SSE frame's `data:` JSON line, ignoring comments and keepalives. */
export function parseSseDataBlock(block: string): unknown | null {
  for (const line of block.split("\n")) {
    if (!line.startsWith("data: ")) continue;
    const payload = line.slice(6).trim();
    if (!payload) return null;
    try {
      return JSON.parse(payload) as unknown;
    } catch {
      return null;
    }
  }
  return null;
}

/** Apply a live ingest_progress snapshot onto a books list (immutable). */
export function patchBooksIngestProgress(
  books: BookResponse[],
  bookId: string,
  stage: string,
  pct: number,
): BookResponse[] {
  const idx = books.findIndex((b) => b.id === bookId);
  if (idx < 0) return books;

  const status =
    stage === "ready" ? "ready" : stage === "failed" ? "failed" : "importing";

  const next = [...books];
  next[idx] = {
    ...books[idx]!,
    status,
    stage,
    progress: pct,
  };
  return next;
}

/** React Query cache updater for a parsed library SSE event. */
export function applyLibraryEventToBooks(
  books: BookResponse[] | undefined,
  event: KinoraEvent,
): BookResponse[] | undefined {
  if (event.event !== "ingest_progress" || !books) return books;
  const stage = typeof event.stage === "string" ? event.stage : "importing";
  const pct = typeof event.pct === "number" ? event.pct : 0;
  return patchBooksIngestProgress(books, event.book_id, stage, pct);
}

/** Whether a library event should trigger a full books refetch. */
export function libraryEventNeedsRefetch(event: KinoraEvent): boolean {
  if (event.event !== "ingest_progress") return false;
  const stage = typeof event.stage === "string" ? event.stage : "";
  return stage === "ready" || stage === "failed";
}

export class LibraryEvents {
  private closedByUser = false;
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private abort: AbortController | null = null;

  constructor(private readonly opts: LibraryEventsOptions) {}

  async connect(): Promise<void> {
    this.closedByUser = false;
    this.opts.onStatus?.("connecting");
    this.abort?.abort();
    const controller = new AbortController();
    this.abort = controller;

    const token = await this.opts.getToken();
    const url = libraryEventsUrl(this.opts.baseUrl, token);

    try {
      const response = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        throw new Error(`library events: ${response.status}`);
      }

      this.attempts = 0;
      this.opts.onStatus?.("open");
      await this.consume(response.body);
    } catch (err) {
      if (controller.signal.aborted && this.closedByUser) return;
      this.opts.onStatus?.("closed");
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
      return;
    }

    if (!this.closedByUser) {
      this.opts.onStatus?.("closed");
      if (this.opts.reconnect ?? true) this.scheduleReconnect();
    }
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.abort?.abort();
    this.abort = null;
  }

  private async consume(body: ReadableStream<Uint8Array>): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (!this.closedByUser) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const raw = parseSseDataBlock(frame);
        if (raw == null) continue;
        const event = parseSessionEvent(raw);
        if (event) this.opts.onEvent(event);
      }
    }
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
