/**
 * SessionSocket — the bidirectional Director/event channel over the backend's
 * `WS /api/ws/sessions/{id}` endpoint (§5.6). We use the WebSocket transport
 * (not SSE) because both shells expose a native `WebSocket`, so no polyfill
 * split is needed; the constructor is *injected* so this module stays free of
 * any DOM/RN lib dependency and is unit-testable with a fake socket.
 *
 * It fans incoming events out through `onEvent` (parsed + validated by
 * {@link parseSessionEvent}) and sends the §5.6 client→backend messages
 * (`intent_update`, `seek`, `comment`), with exponential-backoff reconnect.
 */
import { type TokenProvider } from "../api/client";
import { parseSessionEvent, type KinoraEvent } from "../events";

/** The structural subset of `WebSocket` we use — satisfied by DOM and RN alike. */
export interface WebSocketLike {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
}

export type WebSocketFactory = (url: string) => WebSocketLike;
export type SocketStatus = "connecting" | "open" | "closed";

export interface SessionSocketOptions {
  /** Backend base URL, e.g. `https://api.kinora.app` (http(s) -> ws(s) derived). */
  baseUrl: string;
  sessionId: string;
  getToken: TokenProvider;
  /** `(url) => new WebSocket(url)` — injected so core needs no WebSocket lib. */
  createWebSocket: WebSocketFactory;
  onEvent: (event: KinoraEvent) => void;
  onStatus?: (status: SocketStatus) => void;
  reconnect?: boolean;
  reconnectBaseMs?: number;
}

const MAX_RECONNECT_MS = 10_000;

function toWsUrl(baseUrl: string, path: string, token?: string | null): string {
  const scheme = baseUrl.replace(/^http/i, "ws").replace(/\/+$/, "");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${scheme}${path}${query}`;
}

export class SessionSocket {
  private ws: WebSocketLike | null = null;
  private closedByUser = false;
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly opts: SessionSocketOptions) {}

  async connect(): Promise<void> {
    this.closedByUser = false;
    this.opts.onStatus?.("connecting");
    const token = await this.opts.getToken();
    const url = toWsUrl(this.opts.baseUrl, `/api/ws/sessions/${this.opts.sessionId}`, token);
    const ws = this.opts.createWebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      this.attempts = 0;
      this.opts.onStatus?.("open");
    };
    ws.onmessage = (event) => this.handleMessage(event.data);
    ws.onerror = () => {
      // A close event follows; reconnection is handled there.
    };
    ws.onclose = () => {
      this.ws = null;
      this.opts.onStatus?.("closed");
      if (!this.closedByUser && (this.opts.reconnect ?? true)) this.scheduleReconnect();
    };
  }

  sendIntent(intent: { focusWord: number; velocity: number; mode: string }): void {
    this.send({
      type: "intent_update",
      focus_word: intent.focusWord,
      velocity: intent.velocity,
      mode: intent.mode,
    });
  }

  sendSeek(word: number): void {
    this.send({ type: "seek", word });
  }

  sendComment(note: string, shotId?: string | null): void {
    this.send({ type: "comment", note, shot_id: shotId ?? null });
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.ws?.close();
    this.ws = null;
  }

  private send(payload: Record<string, unknown>): void {
    this.ws?.send(JSON.stringify(payload));
  }

  private handleMessage(data: unknown): void {
    if (typeof data !== "string") return;
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
