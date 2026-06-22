import type { KinoraEvent, KinoraEventType } from "../api/types";

// GenerationClient is pure transport (kinora.md §5.6): it opens the SSE stream
// (EventSource with ?token=, since EventSource can't set headers) — or a
// WebSocket for Director round-trips — parses each server push into a typed
// KinoraEvent, and hands it to `onEvent`. The app wires `onEvent` to the
// zustand events store + the SyncEngine. Implementations are injectable so the
// dispatch can be tested against a mocked EventSource.

export type ConnectionStatus = "idle" | "connecting" | "open" | "closed" | "error";

export const EVENT_TYPES: KinoraEventType[] = [
  "keyframe_ready",
  "clip_ready",
  "scene_stitched",
  "regen_done",
  "budget_low",
  "agent_activity",
  "conflict_choice",
  "ingest_progress",
];

const EVENT_TYPE_SET = new Set<string>(EVENT_TYPES);

export interface GenerationClientConfig {
  sessionId: string;
  eventsUrl: string;
  wsUrl?: string;
  onEvent: (event: KinoraEvent) => void;
  onStatus?: (status: ConnectionStatus) => void;
  /** Inject for tests; defaults to the global EventSource. */
  EventSourceImpl?: typeof EventSource;
  WebSocketImpl?: typeof WebSocket;
  /** Use the WebSocket transport instead of SSE (default: SSE). */
  useWebSocket?: boolean;
  reconnect?: boolean;
  maxBackoffMs?: number;
}

/** Parse a raw SSE/WS payload of a known type into a typed event. */
export function parseEvent(type: string, raw: string): KinoraEvent | null {
  if (!EVENT_TYPE_SET.has(type)) return null;
  try {
    const data = raw ? JSON.parse(raw) : {};
    return { type, data } as KinoraEvent;
  } catch {
    return null;
  }
}

export class GenerationClient {
  private readonly config: GenerationClientConfig;
  private readonly EventSourceImpl?: typeof EventSource;
  private readonly WebSocketImpl?: typeof WebSocket;
  private readonly reconnect: boolean;
  private readonly maxBackoffMs: number;

  private source: EventSource | null = null;
  private socket: WebSocket | null = null;
  private status: ConnectionStatus = "idle";
  private closedByUser = false;
  private backoffMs = 1000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(config: GenerationClientConfig) {
    this.config = config;
    this.EventSourceImpl =
      config.EventSourceImpl ??
      (typeof EventSource !== "undefined" ? EventSource : undefined);
    this.WebSocketImpl =
      config.WebSocketImpl ?? (typeof WebSocket !== "undefined" ? WebSocket : undefined);
    this.reconnect = config.reconnect ?? true;
    this.maxBackoffMs = config.maxBackoffMs ?? 15000;
  }

  connect(): void {
    this.closedByUser = false;
    if (this.config.useWebSocket && this.config.wsUrl && this.WebSocketImpl) {
      this.connectWebSocket();
    } else {
      this.connectSse();
    }
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.source?.close();
    this.source = null;
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
      this.socket = null;
    }
    this.setStatus("closed");
  }

  getStatus(): ConnectionStatus {
    return this.status;
  }

  private setStatus(status: ConnectionStatus): void {
    this.status = status;
    this.config.onStatus?.(status);
  }

  private dispatch(type: string, raw: string): void {
    const event = parseEvent(type, raw);
    if (event) this.config.onEvent(event);
  }

  private connectSse(): void {
    if (!this.EventSourceImpl) {
      this.setStatus("error");
      return;
    }
    this.setStatus("connecting");
    const source = new this.EventSourceImpl(this.config.eventsUrl);
    this.source = source;

    source.onopen = () => this.setStatus("open");
    source.onerror = () => {
      // EventSource reconnects natively; surface the degraded state.
      this.setStatus("error");
    };
    // Named events (event: clip_ready\n data: {...}).
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (ev) =>
        this.dispatch(type, (ev as MessageEvent).data as string),
      );
    }
    // Fallback for backends that send a default-typed message carrying {type}.
    source.onmessage = (ev) => this.handleEnvelope((ev as MessageEvent).data as string);
  }

  private connectWebSocket(): void {
    if (!this.WebSocketImpl || !this.config.wsUrl) {
      this.connectSse();
      return;
    }
    this.setStatus("connecting");
    const socket = new this.WebSocketImpl(this.config.wsUrl);
    this.socket = socket;

    socket.onopen = () => {
      this.backoffMs = 1000;
      this.setStatus("open");
    };
    socket.onmessage = (ev) => this.handleEnvelope(ev.data as string);
    socket.onerror = () => this.setStatus("error");
    socket.onclose = () => {
      this.socket = null;
      if (this.closedByUser || !this.reconnect) {
        this.setStatus("closed");
        return;
      }
      this.scheduleReconnect();
    };
  }

  /** Parse a JSON envelope of the form {type|event, data|payload}. */
  private handleEnvelope(raw: string): void {
    if (!raw) return;
    try {
      const obj = JSON.parse(raw) as Record<string, unknown>;
      const type = (obj.type ?? obj.event) as string | undefined;
      if (!type || !EVENT_TYPE_SET.has(type)) return;
      const data = (obj.data ?? obj.payload ?? obj) as unknown;
      this.config.onEvent({ type, data } as KinoraEvent);
    } catch {
      // ignore malformed frames
    }
  }

  private scheduleReconnect(): void {
    this.setStatus("connecting");
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, this.backoffMs);
    this.backoffMs = Math.min(this.backoffMs * 2, this.maxBackoffMs);
  }
}
