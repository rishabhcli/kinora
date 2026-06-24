/**
 * LibraryEventStream — SSE fan-in for shelf ingest progress (§5.1).
 *
 * Subscribes to `GET /api/books/events` and forwards parsed `ingest_progress`
 * payloads. The constructor injects `createEventSource` so core stays free of
 * DOM/RN dependencies (desktop passes `EventSource`; mobile may omit SSE and
 * rely on polling via {@link useLibraryShelfSync}).
 */
import { ingestProgressEvent } from "../events";

export interface EventSourceLike {
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export interface IngestProgressPayload {
  book_id: string;
  stage?: string | null;
  pct?: number | null;
}

export interface LibraryEventStreamOptions {
  apiBaseUrl: string;
  getToken: () => Promise<string | null>;
  createEventSource: EventSourceFactory;
  onProgress: (payload: IngestProgressPayload) => void;
  onStatus?: (status: "connecting" | "open" | "closed") => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

const MAX_RECONNECT_MS = 10_000;

function libraryEventsUrl(apiBaseUrl: string, token: string | null): string {
  const base = apiBaseUrl.replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${base}/api/books/events${query}`;
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
    const source = this.opts.createEventSource(libraryEventsUrl(this.opts.apiBaseUrl, token));
    this.source = source;

    source.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    source.onmessage = (event) => this.handleMessage(event.data);
    source.onerror = () => {
      this.opts.onStatus?.("closed");
      this.source = null;
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.source?.close();
    this.source = null;
    this.opts.onStatus?.("closed");
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    const base = this.opts.reconnectBaseMs ?? 1_000;
    const delay = Math.min(base * 2 ** this.attempts, MAX_RECONNECT_MS);
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  private handleMessage(raw: string): void {
    if (!raw.trim()) return;
    let payload: unknown;
    try {
      payload = JSON.parse(raw);
    } catch {
      return;
    }
    const parsed = ingestProgressEvent.safeParse(payload);
    if (!parsed.success) return;
    const data = parsed.data as IngestProgressPayload & { pct?: number | null };
    this.opts.onProgress({
      book_id: data.book_id,
      stage: data.stage ?? null,
      pct: data.pct ?? null,
    });
  }
}
