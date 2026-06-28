/**
 * Registers every allowlisted invoke handler on the {@link IpcRouter} and the
 * single `ipcMain.handle` shim that dispatches `kinora:invoke` through it. Also
 * wires the fire-and-forget `kinora:send` channel.
 *
 * This is the one place that bridges the pure router to Electron's `ipcMain`,
 * `dialog`, `shell`, and `Notification`. Each handler is tiny — the real work
 * lives in the services it delegates to — so the security-relevant surface is
 * concentrated and easy to audit.
 */
import { dialog, ipcMain, shell, type IpcMainInvokeEvent } from "electron";
import path from "node:path";
import {
  INVOKE_CHANNELS,
  isSendChannel,
  type AddBookSource,
  type DiagnosticsSnapshot,
  type LogEntry,
  type SystemState,
  type UpdateStatus,
} from "../shared/ipc-contract.js";
import { IpcRouter, v, type IpcCallContext } from "../core/ipc-router.js";
import type { Logger, ScopedLogger } from "../core/logger.js";
import type { SecureStore } from "./secure-store.js";
import type { WindowManager } from "./window-manager.js";
import type { Diagnostics } from "./diagnostics.js";
import type { AutoUpdateService } from "./auto-update.js";
import type { SystemMonitors } from "./monitors.js";
import type { ConfigStore } from "../core/config-store.js";

export interface PrefsConfig {
  [key: string]: unknown;
}

export interface IpcHandlerDeps {
  router: IpcRouter;
  logger: Logger;
  log: ScopedLogger;
  secureStore: SecureStore;
  windows: WindowManager;
  diagnostics: Diagnostics;
  updater: AutoUpdateService;
  monitors: SystemMonitors;
  prefs: ConfigStore<PrefsConfig>;
  pickBook: () => Promise<string | null>;
  notify: (title: string, body: string) => void;
  onRendererReady: () => void;
  allowedOrigins: string[];
}

/**
 * Register all invoke handlers + the dispatch shim. Idempotent guards aren't
 * needed because main calls this exactly once after `app.whenReady`.
 */
export function registerIpcHandlers(deps: IpcHandlerDeps): void {
  const { router } = deps;

  router.handle("kinora:pick-book", () => deps.pickBook());

  router.handle(
    "kinora:notify",
    ({ title, body }) => {
      deps.notify(title, body);
    },
    v.notify,
  );

  router.handle("kinora:token:get", () => deps.secureStore.getToken());
  router.handle(
    "kinora:token:set",
    ({ token }) => {
      if (token === null) {
        deps.secureStore.clear();
        return { ok: true };
      }
      return { ok: deps.secureStore.setToken(token) };
    },
    v.tokenSet,
  );
  router.handle("kinora:token:clear", () => {
    deps.secureStore.clear();
    return { ok: true };
  });

  router.handle("kinora:prefs:get", ({ key }) => deps.prefs.get(key), v.prefsGet);
  router.handle(
    "kinora:prefs:set",
    ({ key, value }) => {
      deps.prefs.set(key, value);
      return { ok: true };
    },
    v.prefsSet,
  );

  router.handle("kinora:diagnostics", (): DiagnosticsSnapshot => deps.diagnostics.snapshot());
  router.handle("kinora:logs:tail", (payload): LogEntry[] => deps.logger.tail(payload?.limit), v.logsTail);
  router.handle("kinora:system:state", (): SystemState => deps.monitors.current());

  router.handle("kinora:update:check", (): Promise<UpdateStatus> => deps.updater.checkNow());
  router.handle("kinora:update:install", () => ({ ok: deps.updater.installNow() }));

  router.handle(
    "kinora:window:open",
    (payload) => {
      const win = deps.windows.createWindow(payload?.route);
      return { id: win.webContents.id };
    },
    v.windowOpen,
  );

  router.handle(
    "kinora:open-external",
    ({ url }) => {
      // Only http(s)/mailto may escape to the OS browser.
      if (!/^https?:|^mailto:/i.test(url)) {
        deps.log.warn("open-external rejected", { url });
        return { ok: false };
      }
      void shell.openExternal(url);
      return { ok: true };
    },
    v.openExternal,
  );

  // Fail fast in dev if a channel lost its handler.
  const missing = router.missing(INVOKE_CHANNELS);
  if (missing.length > 0) {
    deps.log.error("ipc: channels missing handlers", { missing });
  }

  // The single dispatch shim. The renderer always calls `kinora:invoke` with a
  // (channel, payload) pair; the router enforces the allowlist + validation.
  ipcMain.handle("kinora:invoke", async (event: IpcMainInvokeEvent, channel: unknown, payload: unknown) => {
    const ctx: IpcCallContext = { senderId: event.sender.id, senderUrl: senderUrl(event) };
    const result = await router.dispatch(channel, payload, ctx);
    if (result.ok) return result.value;
    // Surface only the message; the structured error was already logged.
    throw new Error(result.error.message);
  });

  // Fire-and-forget renderer → main messages.
  ipcMain.on("kinora:send", (event, channel: unknown, payload: unknown) => {
    if (!router.isOriginAllowed(senderUrl(event))) {
      deps.log.warn("send: forbidden origin", { origin: senderUrl(event) });
      return;
    }
    if (!isSendChannel(channel)) {
      deps.log.warn("send: unknown channel", { channel: String(channel) });
      return;
    }
    if (channel === "kinora:renderer-ready") {
      deps.onRendererReady();
    } else if (channel === "kinora:renderer-error") {
      const p = payload as { message?: unknown; stack?: unknown };
      deps.log.error("renderer error", {
        message: String(p?.message ?? ""),
        stack: typeof p?.stack === "string" ? p.stack : undefined,
      });
    }
  });
}

function senderUrl(event: IpcMainInvokeEvent | Electron.IpcMainEvent): string {
  try {
    return event.sender.getURL();
  } catch {
    return "";
  }
}

/** Build the native book picker used by both the menu and the IPC handler. */
export function makeBookPicker(notify: (t: string, b: string) => void): () => Promise<string | null> {
  return async () => {
    const res = await dialog.showOpenDialog({
      title: "Add a book to Kinora",
      buttonLabel: "Add Book",
      properties: ["openFile"],
      filters: [{ name: "Books", extensions: ["pdf", "epub"] }],
    });
    if (res.canceled || res.filePaths.length === 0) return null;
    const file = res.filePaths[0];
    notify("Book added", `${path.basename(file)} — generating its film…`);
    return file;
  };
}

export type { AddBookSource };
