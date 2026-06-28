/**
 * Preload — the ONLY bridge between the sandboxed renderer and the main process.
 *
 * Runs in the isolated world with `contextIsolation` + `sandbox` on, so it can
 * use a curated slice of Electron (`ipcRenderer`, `contextBridge`) but the page
 * cannot. We expose exactly one frozen object, `window.kinora`, whose every
 * method funnels through the single `kinora:invoke` / `kinora:send` channels —
 * the main-process router then enforces the allowlist + payload validation.
 * No raw `ipcRenderer` is ever handed to the page (that would let any script
 * post to arbitrary channels).
 *
 * Backwards-compat: the original `window.__KINORA_NATIVE__` flag is preserved so
 * the renderer's existing translucency CSS path keeps working in the Electron
 * host (macOS vibrancy / Windows 11 acrylic — OS material, NOT Liquid Glass).
 */
import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";
import {
  EVENT_CHANNELS,
  type DeepLink,
  type DiagnosticsSnapshot,
  type EventChannel,
  type EventChannels,
  type InvokeChannel,
  type InvokeChannels,
  type LogEntry,
  type SendChannel,
  type SystemState,
  type UpdateStatus,
} from "./shared/ipc-contract.js";

const hasNativeGlass = process.platform === "darwin" || process.platform === "win32";

/** Strongly-typed invoke that always goes through the single dispatch channel. */
function invoke<C extends InvokeChannel>(
  channel: C,
  payload?: InvokeChannels[C]["request"],
): Promise<InvokeChannels[C]["response"]> {
  return ipcRenderer.invoke("kinora:invoke", channel, payload);
}

/** Fire-and-forget send through the single send channel. */
function send(channel: SendChannel, payload?: unknown): void {
  ipcRenderer.send("kinora:send", channel, payload);
}

/**
 * Subscribe to a main→renderer broadcast. Returns an unsubscribe function. The
 * Electron event object is stripped so the page only sees the payload (never a
 * handle to `sender`, which could be abused).
 */
function subscribe<C extends EventChannel>(
  channel: C,
  listener: (payload: EventChannels[C]["payload"]) => void,
): () => void {
  if (!(EVENT_CHANNELS as readonly string[]).includes(channel)) {
    // Defensive: never wire a listener to a non-allowlisted channel.
    return () => {};
  }
  const wrapped = (_e: IpcRendererEvent, payload: EventChannels[C]["payload"]) => listener(payload);
  ipcRenderer.on(channel, wrapped as (e: IpcRendererEvent, ...args: unknown[]) => void);
  return () => ipcRenderer.removeListener(channel, wrapped as (e: IpcRendererEvent, ...args: unknown[]) => void);
}

/** The public bridge surface. Mirrors the native Swift shell's `window.kinora`. */
const bridge = {
  /** True when backed by native OS glass material (vibrancy / acrylic). */
  isNativeGlass: hasNativeGlass,
  platform: process.platform,

  // --- Library / books -----------------------------------------------------
  pickBook: () => invoke("kinora:pick-book"),
  /** Compat alias mirroring the native shell's `openBook` entry point. */
  openBook: () => invoke("kinora:pick-book"),
  notify: (title: string, body: string) => invoke("kinora:notify", { title, body }),
  onAddBook: (cb: (payload: EventChannels["kinora:add-book"]["payload"]) => void) =>
    subscribe("kinora:add-book", cb),

  // --- Auth token (safeStorage-backed) -------------------------------------
  token: {
    get: (): Promise<string | null> => invoke("kinora:token:get"),
    set: (token: string | null) => invoke("kinora:token:set", { token }),
    clear: () => invoke("kinora:token:clear"),
  },

  // --- Renderer preferences (persisted JSON) -------------------------------
  prefs: {
    get: <T = unknown>(key: string): Promise<T> => invoke("kinora:prefs:get", { key }) as Promise<T>,
    set: (key: string, value: unknown) => invoke("kinora:prefs:set", { key, value }),
  },

  // --- Deep links ----------------------------------------------------------
  onDeepLink: (cb: (link: DeepLink) => void) => subscribe("kinora:deep-link", cb),

  // --- System (power / network) --------------------------------------------
  system: {
    state: (): Promise<SystemState> => invoke("kinora:system:state"),
    onChange: (cb: (s: SystemState) => void) => subscribe("kinora:system:changed", cb),
  },

  // --- Auto-update ---------------------------------------------------------
  update: {
    check: (): Promise<UpdateStatus> => invoke("kinora:update:check"),
    install: () => invoke("kinora:update:install"),
    onStatus: (cb: (s: UpdateStatus) => void) => subscribe("kinora:update:status", cb),
  },

  // --- Windows -------------------------------------------------------------
  openWindow: (route?: string) => invoke("kinora:window:open", { route }),
  openExternal: (url: string) => invoke("kinora:open-external", { url }),

  // --- Menu actions (open settings/diagnostics/…) --------------------------
  onMenuAction: (cb: (payload: EventChannels["kinora:menu-action"]["payload"]) => void) =>
    subscribe("kinora:menu-action", cb),

  // --- Diagnostics ---------------------------------------------------------
  diagnostics: (): Promise<DiagnosticsSnapshot> => invoke("kinora:diagnostics"),
  logs: (limit?: number): Promise<LogEntry[]> => invoke("kinora:logs:tail", { limit }),

  // --- Lifecycle -----------------------------------------------------------
  /** The renderer must call this once mounted so queued deep links flush. */
  ready: () => send("kinora:renderer-ready"),
  reportError: (message: string, stack?: string) => send("kinora:renderer-error", { message, stack }),
} as const;

export type KinoraBridge = typeof bridge;

contextBridge.exposeInMainWorld("kinora", bridge);

if (hasNativeGlass) {
  // Preserve the original flag the renderer's translucency CSS path reads, so
  // one code path (`html.kinora-native`) drives translucency in every native
  // host (Electron vibrancy/acrylic AND the Swift shell).
  contextBridge.exposeInMainWorld("__KINORA_NATIVE__", true);
}
