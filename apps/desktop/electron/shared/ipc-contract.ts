/**
 * The single source of truth for the main↔renderer IPC surface.
 *
 * Both the preload bridge and the main-process router import this module, so
 * the channel names, directions, and payload shapes can never drift apart.
 * Nothing here touches `electron` — it is plain TypeScript so the pure router
 * and the preload (which only `contextBridge`-exposes a derived API) can share
 * it, and so the whole contract is unit-testable without launching Electron.
 *
 * Security model (least privilege): the renderer can only reach channels that
 * appear in one of the three frozen allowlists below. Anything else is dropped
 * by the router with a structured warning. Channels are namespaced `kinora:`
 * so they never collide with Electron's internal `ELECTRON_*` channels.
 */

/** Prefix every Kinora channel shares — used to validate inbound names. */
export const CHANNEL_PREFIX = "kinora:" as const;

// ---------------------------------------------------------------------------
// invoke/handle — request→response (renderer awaits a value from main).
// ---------------------------------------------------------------------------

export interface InvokeChannels {
  /** Open the native book picker; resolves to the chosen path or null. */
  "kinora:pick-book": { request: void; response: string | null };
  /** Raise a native OS notification. */
  "kinora:notify": { request: { title: string; body: string }; response: void };
  /** Read the persisted auth token (decrypted via safeStorage). */
  "kinora:token:get": { request: void; response: string | null };
  /** Persist the auth token (encrypted via safeStorage when available). */
  "kinora:token:set": { request: { token: string | null }; response: { ok: boolean } };
  /** Clear any persisted auth token (logout). */
  "kinora:token:clear": { request: void; response: { ok: boolean } };
  /** Read a renderer-scoped preference value. */
  "kinora:prefs:get": { request: { key: string }; response: unknown };
  /** Write a renderer-scoped preference value. */
  "kinora:prefs:set": { request: { key: string; value: unknown }; response: { ok: boolean } };
  /** Snapshot of host/runtime info for the diagnostics panel. */
  "kinora:diagnostics": { request: void; response: DiagnosticsSnapshot };
  /** Tail of the structured in-memory log ring for the diagnostics panel. */
  "kinora:logs:tail": { request: { limit?: number }; response: LogEntry[] };
  /** Current network + power state. */
  "kinora:system:state": { request: void; response: SystemState };
  /** Ask the updater to check now; resolves with the current update status. */
  "kinora:update:check": { request: void; response: UpdateStatus };
  /** Quit and install a downloaded update (no-op if none ready). */
  "kinora:update:install": { request: void; response: { ok: boolean } };
  /** Open a new renderer window (optionally deep-linking to a route). */
  "kinora:window:open": { request: { route?: string }; response: { id: number } };
  /** Open an external URL in the user's default browser (validated). */
  "kinora:open-external": { request: { url: string }; response: { ok: boolean } };
}

// ---------------------------------------------------------------------------
// send (renderer → main, fire-and-forget).
// ---------------------------------------------------------------------------

export interface SendChannels {
  /** Renderer announces it has booted (used to flush queued deep-links). */
  "kinora:renderer-ready": { payload: void };
  /** Renderer reports an unhandled error so main can log/aggregate it. */
  "kinora:renderer-error": { payload: { message: string; stack?: string } };
}

// ---------------------------------------------------------------------------
// on (main → renderer broadcasts the renderer can subscribe to).
// ---------------------------------------------------------------------------

export interface EventChannels {
  /** A book file was opened via the OS (file-open / drop / picker). */
  "kinora:add-book": { payload: { path: string; source: AddBookSource } };
  /** A `kinora://` deep link was activated. */
  "kinora:deep-link": { payload: DeepLink };
  /** Network/power state changed. */
  "kinora:system:changed": { payload: SystemState };
  /** Update lifecycle progressed (checking → available → downloaded …). */
  "kinora:update:status": { payload: UpdateStatus };
  /** A menu/shortcut action the renderer should react to (e.g. open settings). */
  "kinora:menu-action": { payload: { action: MenuAction } };
}

// ---------------------------------------------------------------------------
// Payload value types
// ---------------------------------------------------------------------------

export type AddBookSource = "picker" | "file-open" | "drop" | "deep-link" | "cli";

export type MenuAction =
  | "open-settings"
  | "open-diagnostics"
  | "open-library"
  | "toggle-director-bar"
  | "new-window"
  | "check-for-updates";

export interface DeepLink {
  /** The host segment, e.g. `book`, `open`, `auth`. */
  action: string;
  /** Path segments after the host, already URI-decoded. */
  segments: string[];
  /** Query parameters as a flat string map. */
  params: Record<string, string>;
  /** The original href, preserved for logging. */
  href: string;
}

export interface DiagnosticsSnapshot {
  appName: string;
  appVersion: string;
  electron: string;
  chrome: string;
  node: string;
  v8: string;
  platform: NodeJS.Platform;
  arch: string;
  osRelease: string;
  locale: string;
  uptimeSec: number;
  memory: { rssMB: number; heapUsedMB: number; heapTotalMB: number };
  windows: number;
  packaged: boolean;
  liveVideo: boolean;
  apiBaseUrl: string;
  logCount: number;
}

export type LogLevel = "debug" | "info" | "warn" | "error";

export interface LogEntry {
  ts: number;
  level: LogLevel;
  scope: string;
  message: string;
  data?: Record<string, unknown>;
}

export interface SystemState {
  online: boolean;
  onBattery: boolean;
  /** macOS/Win only; "unknown" when unavailable. */
  thermalState: "nominal" | "fair" | "serious" | "critical" | "unknown";
  /** True while the system is preparing to sleep / suspended. */
  suspended: boolean;
}

export type UpdatePhase =
  | "idle"
  | "checking"
  | "available"
  | "not-available"
  | "downloading"
  | "downloaded"
  | "error"
  | "disabled";

export interface UpdateStatus {
  phase: UpdatePhase;
  version?: string;
  /** 0–100 while downloading. */
  percent?: number;
  /** Bytes/sec while downloading. */
  bytesPerSecond?: number;
  message?: string;
  /** True when this build is configured to receive a staged rollout. */
  stagedRollout?: boolean;
}

// ---------------------------------------------------------------------------
// Frozen allowlists — the ONLY channels the bridge/router accept.
// ---------------------------------------------------------------------------

export const INVOKE_CHANNELS = Object.freeze([
  "kinora:pick-book",
  "kinora:notify",
  "kinora:token:get",
  "kinora:token:set",
  "kinora:token:clear",
  "kinora:prefs:get",
  "kinora:prefs:set",
  "kinora:diagnostics",
  "kinora:logs:tail",
  "kinora:system:state",
  "kinora:update:check",
  "kinora:update:install",
  "kinora:window:open",
  "kinora:open-external",
] as const) satisfies ReadonlyArray<keyof InvokeChannels>;

export const SEND_CHANNELS = Object.freeze([
  "kinora:renderer-ready",
  "kinora:renderer-error",
] as const) satisfies ReadonlyArray<keyof SendChannels>;

export const EVENT_CHANNELS = Object.freeze([
  "kinora:add-book",
  "kinora:deep-link",
  "kinora:system:changed",
  "kinora:update:status",
  "kinora:menu-action",
] as const) satisfies ReadonlyArray<keyof EventChannels>;

export type InvokeChannel = keyof InvokeChannels;
export type SendChannel = keyof SendChannels;
export type EventChannel = keyof EventChannels;

const INVOKE_SET: ReadonlySet<string> = new Set(INVOKE_CHANNELS);
const SEND_SET: ReadonlySet<string> = new Set(SEND_CHANNELS);
const EVENT_SET: ReadonlySet<string> = new Set(EVENT_CHANNELS);

export function isInvokeChannel(name: unknown): name is InvokeChannel {
  return typeof name === "string" && INVOKE_SET.has(name);
}
export function isSendChannel(name: unknown): name is SendChannel {
  return typeof name === "string" && SEND_SET.has(name);
}
export function isEventChannel(name: unknown): name is EventChannel {
  return typeof name === "string" && EVENT_SET.has(name);
}

/** The protocol Kinora registers for OS deep-links. */
export const KINORA_PROTOCOL = "kinora" as const;
