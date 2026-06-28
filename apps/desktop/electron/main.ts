/**
 * Kinora desktop — main-process entry point.
 *
 * This file is now a thin *orchestrator*: it constructs the structured logger,
 * config stores, and the service objects (window manager, IPC router/handlers,
 * secure token store, auto-update, monitors, diagnostics, menu/tray/shortcuts,
 * deep-link protocol), wires them together, and drives the Electron app
 * lifecycle. All non-trivial logic lives in `electron/core` (pure, unit-tested)
 * and `electron/services` (Electron-bound). See electron/DESIGN.md.
 *
 * Native window glass is the same as before — macOS `vibrancy` / Windows 11
 * `backgroundMaterial: "acrylic"`. This is OS material translucency, NOT
 * Liquid Glass (which is native-SDK-26 only; Electron can't render it).
 */
import { Notification, app, nativeImage } from "electron";
import path from "node:path";

import { readAppConfig } from "./core/app-config.js";
import { Logger, createConsoleSink } from "./core/logger.js";
import { ConfigStore } from "./core/config-store.js";
import { createFileAdapter } from "./core/fs-adapter.js";
import { IpcRouter } from "./core/ipc-router.js";
import { deepLinkToRoute } from "./core/deep-link.js";
import { WindowManager, resolveAppPaths, type WindowManagerConfig } from "./services/window-manager.js";
import { SecureStore } from "./services/secure-store.js";
import { SystemMonitors } from "./services/monitors.js";
import { Diagnostics } from "./services/diagnostics.js";
import { AutoUpdateService } from "./services/auto-update.js";
import { ProtocolService } from "./services/protocol.js";
import { MenuService } from "./services/menu.js";
import { findDuplicateAccelerators } from "./core/menu-template.js";
import { makeBookPicker, registerIpcHandlers, type PrefsConfig } from "./services/ipc-handlers.js";
import type { AddBookSource, MenuAction, SystemState, UpdateStatus } from "./shared/ipc-contract.js";

const VITE_DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL ?? null;
const cfg = readAppConfig(process.env);

// Brand the app as "Kinora" before `ready` so the macOS app menu / About panel
// / dock read "Kinora" rather than "Electron".
app.setName("Kinora");

const { preloadPath, indexHtml, iconPath } = resolveAppPaths(__dirname, VITE_DEV_SERVER_URL);
const APP_ICON = nativeImage.createFromPath(iconPath);

const ALLOWED_ORIGINS = [
  ...(VITE_DEV_SERVER_URL ? [VITE_DEV_SERVER_URL] : []),
  "http://localhost:5173",
  "file://",
];

// ---------------------------------------------------------------------------
// Logger + persisted stores
// ---------------------------------------------------------------------------
const logger = new Logger({ level: cfg.logLevel, sinks: [createConsoleSink()] });
const log = logger.scoped("main");

function userDataFile(name: string): string {
  return path.join(app.getPath("userData"), name);
}

let windowStore: ConfigStore<WindowManagerConfig>;
let prefsStore: ConfigStore<PrefsConfig>;
let windows: WindowManager;
let secureStore: SecureStore;
let monitors: SystemMonitors;
let diagnostics: Diagnostics;
let updater: AutoUpdateService;
let protocolSvc: ProtocolService;
let menuSvc: MenuService;

function notify(title: string, body: string): void {
  if (Notification.isSupported()) new Notification({ title, body, silent: false }).show();
}

function emitMenuAction(action: MenuAction): void {
  if (action === "new-window") {
    windows.createWindow();
    return;
  }
  if (action === "check-for-updates") {
    void updater.checkNow();
    return;
  }
  windows.broadcast("kinora:menu-action", { action });
}

async function addBookViaPicker(): Promise<void> {
  const file = await pickBook();
  if (file) windows.broadcast("kinora:add-book", { path: file, source: "picker" as AddBookSource });
}

const pickBook = makeBookPicker(notify);

// ---------------------------------------------------------------------------
// Single-instance + deep-link bootstrapping (must run before `ready`).
// ---------------------------------------------------------------------------
function bootstrapProtocol(): boolean {
  protocolSvc = new ProtocolService({
    log: logger.scoped("protocol"),
    onDeepLink: (link) => {
      const route = deepLinkToRoute(link);
      windows.broadcast("kinora:deep-link", link);
      if (route) log.info("deep-link routed", { route });
    },
    onOpenBook: (filePath, source) => {
      windows.broadcast("kinora:add-book", { path: filePath, source });
    },
    onFocusRequested: () => {
      const win = windows?.focused();
      if (win) {
        if (win.isMinimized()) win.restore();
        win.focus();
      }
    },
  });

  const gotLock = protocolSvc.acquireSingleInstance();
  if (!gotLock) {
    log.info("another instance owns the lock; quitting");
    app.quit();
    return false;
  }
  protocolSvc.registerProtocol();
  if (process.platform === "darwin") protocolSvc.wireMacEvents();
  return true;
}

// ---------------------------------------------------------------------------
// Wire everything once Electron is ready.
// ---------------------------------------------------------------------------
function buildServices(): void {
  windowStore = new ConfigStore<WindowManagerConfig>({
    file: createFileAdapter(userDataFile("window-state.json")),
    defaults: { windowState: null },
    onLog: (level, message, data) => logger.log(level, "window-store", message, data),
  });
  prefsStore = new ConfigStore<PrefsConfig>({
    file: createFileAdapter(userDataFile("prefs.json")),
    defaults: {},
    onLog: (level, message, data) => logger.log(level, "prefs-store", message, data),
  });

  windows = new WindowManager({
    store: windowStore,
    log: logger.scoped("windows"),
    preloadPath,
    devServerUrl: VITE_DEV_SERVER_URL,
    indexHtml,
    icon: APP_ICON,
    allowedOrigins: ALLOWED_ORIGINS,
  });

  secureStore = new SecureStore({
    file: createFileAdapter(userDataFile("auth.json")),
    log: logger.scoped("secure-store"),
  });

  monitors = new SystemMonitors({
    log: logger.scoped("monitors"),
    onChange: (state: SystemState) => windows.broadcast("kinora:system:changed", state),
  });

  diagnostics = new Diagnostics({
    logger,
    log: logger.scoped("diagnostics"),
    apiBaseUrl: cfg.apiBaseUrl,
    liveVideo: cfg.liveVideo,
    windowCount: () => windows.count,
    submitUrl: cfg.crashSubmitUrl,
  });

  updater = new AutoUpdateService({
    log: logger.scoped("auto-update"),
    config: cfg.rollout,
    onStatus: (status: UpdateStatus) => windows.broadcast("kinora:update:status", status),
  });

  menuSvc = new MenuService({
    log: logger.scoped("menu"),
    icon: APP_ICON,
    addBook: () => void addBookViaPicker(),
    menuAction: emitMenuAction,
    openExternal: (url) => {
      void import("electron").then(({ shell }) => shell.openExternal(url));
    },
    checkForUpdates: () => void updater.checkNow(),
  });

  const router = new IpcRouter({
    allowedOriginPrefixes: ALLOWED_ORIGINS,
    onLog: (level, message, data) => logger.log(level, "ipc", message, data),
  });

  registerIpcHandlers({
    router,
    logger,
    log: logger.scoped("ipc"),
    secureStore,
    windows,
    diagnostics,
    updater,
    monitors,
    prefs: prefsStore,
    pickBook,
    notify,
    onRendererReady: () => protocolSvc.markRendererReady(),
    allowedOrigins: ALLOWED_ORIGINS,
  });
}

app.whenReady().then(() => {
  if (!bootstrapProtocol()) return;

  const dupes = findDuplicateAccelerators();
  if (dupes.length > 0) log.warn("duplicate accelerators", { dupes });

  app.setAboutPanelOptions({
    applicationName: "Kinora",
    copyright: "© Kinora — Where stories come to life.",
  });
  if (process.platform === "darwin" && !APP_ICON.isEmpty()) app.dock?.setIcon(APP_ICON);

  buildServices();

  diagnostics.start();
  menuSvc.installMenu();
  menuSvc.registerGlobalShortcuts();
  if (cfg.enableTray) menuSvc.installTray();
  monitors.start();
  updater.start();

  windows.createPrimary();
  protocolSvc.consumeLaunchArgs(process.argv);

  notify("Kinora is ready", "Open a book to watch its AI-generated film.");
  log.info("ready", { packaged: app.isPackaged, apiBaseUrl: cfg.apiBaseUrl, liveVideo: cfg.liveVideo });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (windows && windows.count === 0) windows.createPrimary();
});

app.on("before-quit", () => {
  try {
    windows?.flush();
    prefsStore?.flush();
    monitors?.stop();
    updater?.stop();
    menuSvc?.dispose();
    void logger.flush();
  } catch (err) {
    log.warn("before-quit cleanup error", { message: err instanceof Error ? err.message : String(err) });
  }
});
