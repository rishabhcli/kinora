/**
 * Multi-window lifecycle + per-window state persistence.
 *
 * Wraps `BrowserWindow` creation with: the native-glass options from the
 * original main.ts (macOS vibrancy / Win 11 acrylic), restore-on-launch of the
 * primary window's bounds (via the pure `window-state` reconciler), a hardened
 * `webPreferences` block, and a guard that blocks in-app navigation away from
 * the app origin (defence against a compromised renderer opening arbitrary
 * URLs). New windows cascade off the focused one.
 */
import { BrowserWindow, screen, shell } from "electron";
import path from "node:path";
import type { ConfigStore } from "../core/config-store.js";
import type { ScopedLogger } from "../core/logger.js";
import {
  cascadeFrom,
  reconcileWindowState,
  type Bounds,
  type DisplayRect,
  type WindowState,
} from "../core/window-state.js";

export interface WindowManagerConfig {
  windowState: WindowState | null;
  [key: string]: unknown;
}

export interface WindowManagerDeps {
  store: ConfigStore<WindowManagerConfig>;
  log: ScopedLogger;
  preloadPath: string;
  /** Dev server URL (when set) or null for packaged file load. */
  devServerUrl: string | null;
  /** Absolute path to the packaged index.html. */
  indexHtml: string;
  /** Native-image app icon (already resolved). */
  icon: Electron.NativeImage;
  /** Allowed in-app navigation origin prefixes. */
  allowedOrigins: string[];
}

export class WindowManager {
  private readonly deps: WindowManagerDeps;
  private readonly windows = new Set<BrowserWindow>();
  private saveTimer: NodeJS.Timeout | null = null;

  constructor(deps: WindowManagerDeps) {
    this.deps = deps;
  }

  get count(): number {
    return this.windows.size;
  }

  all(): BrowserWindow[] {
    return [...this.windows];
  }

  focused(): BrowserWindow | null {
    return BrowserWindow.getFocusedWindow() ?? this.all()[0] ?? null;
  }

  /** Create the first (primary) window, restoring persisted bounds. */
  createPrimary(): BrowserWindow {
    const displays = this.displayRects();
    const restored = reconcileWindowState(this.deps.store.get("windowState"), displays);
    const win = this.spawn(restored.bounds, { restorePrimary: restored });
    return win;
  }

  /** Create an additional window, cascading off the focused one. */
  createWindow(route?: string): BrowserWindow {
    const focused = this.focused();
    const displays = this.displayRects();
    const area = (displays[0] ?? { bounds: { x: 0, y: 0, width: 1280, height: 800 }, id: 0 }).bounds;
    let bounds: Bounds;
    if (focused && !focused.isDestroyed()) {
      const fb = focused.getBounds();
      bounds = cascadeFrom(fb, area);
    } else {
      bounds = reconcileWindowState(null, displays).bounds;
    }
    return this.spawn(bounds, { route });
  }

  private spawn(
    bounds: Bounds,
    opts: { route?: string; restorePrimary?: WindowState },
  ): BrowserWindow {
    const isMac = process.platform === "darwin";
    const isWin = process.platform === "win32";

    const win = new BrowserWindow({
      ...bounds,
      minWidth: 900,
      minHeight: 600,
      title: "Kinora",
      icon: this.deps.icon,
      show: false,
      frame: true,
      backgroundColor: isMac || isWin ? "#00000000" : "#1A1615",
      ...(isMac ? { vibrancy: "under-window" as const, visualEffectState: "active" as const, titleBarStyle: "hidden" as const } : {}),
      ...(isWin ? { backgroundMaterial: "acrylic" as const } : {}),
      webPreferences: {
        preload: this.deps.preloadPath,
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        allowRunningInsecureContent: false,
        spellcheck: true,
      },
    });

    // macOS uses a hidden title bar (no grey title-bar strip). Hide the native
    // traffic lights by default; the renderer reveals them on hover over the top
    // bar via the kinora:window:traffic-lights channel. No-op on other platforms.
    if (isMac) win.setWindowButtonVisibility(false);

    this.windows.add(win);
    this.hardenNavigation(win);
    this.wireStatePersistence(win, Boolean(opts.restorePrimary));

    win.once("ready-to-show", () => {
      if (opts.restorePrimary?.maximized) win.maximize();
      if (opts.restorePrimary?.fullScreen) win.setFullScreen(true);
      win.show();
    });

    win.on("closed", () => {
      this.windows.delete(win);
    });

    const url = this.deps.devServerUrl;
    const hash = opts.route ? `#${opts.route.replace(/^#/, "")}` : "";
    if (url) {
      void win.loadURL(`${url}${hash}`);
    } else {
      void win.loadFile(this.deps.indexHtml, hash ? { hash: hash.slice(1) } : undefined);
    }

    return win;
  }

  /**
   * Block the renderer from navigating to or opening external origins inside an
   * app window. Same-origin nav is allowed; everything else opens in the user's
   * default browser (and only if it's http/https/mailto).
   */
  private hardenNavigation(win: BrowserWindow): void {
    const allowed = this.deps.allowedOrigins;
    const isAllowed = (target: string) => allowed.some((p) => target.startsWith(p));

    win.webContents.on("will-navigate", (event, url) => {
      if (!isAllowed(url)) {
        event.preventDefault();
        this.deps.log.warn("blocked in-app navigation", { url });
      }
    });

    win.webContents.setWindowOpenHandler(({ url }) => {
      if (/^https?:|^mailto:/.test(url)) {
        void shell.openExternal(url);
      } else {
        this.deps.log.warn("blocked window.open", { url });
      }
      return { action: "deny" };
    });

    // Strip the ability to attach a webview tag entirely.
    win.webContents.on("will-attach-webview", (event) => {
      event.preventDefault();
      this.deps.log.warn("blocked webview attach");
    });
  }

  /** Persist the PRIMARY window's bounds (debounced) on move/resize/close. */
  private wireStatePersistence(win: BrowserWindow, isPrimary: boolean): void {
    if (!isPrimary) return;
    const save = () => {
      if (this.saveTimer) clearTimeout(this.saveTimer);
      this.saveTimer = setTimeout(() => this.persist(win), 400);
    };
    win.on("resize", save);
    win.on("move", save);
    win.on("maximize", () => this.persist(win));
    win.on("unmaximize", () => this.persist(win));
    win.on("enter-full-screen", () => this.persist(win));
    win.on("leave-full-screen", () => this.persist(win));
    win.on("close", () => this.persist(win));
  }

  private persist(win: BrowserWindow): void {
    if (win.isDestroyed()) return;
    // When maximised/fullscreen, getBounds() is the inflated size; prefer the
    // normal bounds so a later un-maximise restores correctly.
    const normal = win.getNormalBounds();
    const state: WindowState = {
      bounds: normal,
      maximized: win.isMaximized(),
      fullScreen: win.isFullScreen(),
    };
    this.deps.store.set("windowState", state);
  }

  private displayRects(): DisplayRect[] {
    try {
      const primary = screen.getPrimaryDisplay();
      const all = screen.getAllDisplays();
      // Put the primary first so the reconciler centres there by default.
      const ordered = [primary, ...all.filter((d) => d.id !== primary.id)];
      return ordered.map((d) => ({ id: d.id, bounds: d.workArea }));
    } catch {
      return [{ id: 0, bounds: { x: 0, y: 0, width: 1280, height: 800 } }];
    }
  }

  /** Broadcast an event to every live window's renderer. */
  broadcast(channel: string, payload: unknown): void {
    for (const win of this.windows) {
      if (!win.isDestroyed()) win.webContents.send(channel, payload);
    }
  }

  flush(): void {
    const primary = this.all()[0];
    if (primary && !primary.isDestroyed()) this.persist(primary);
    this.deps.store.flush();
  }
}

/** Resolve the packaged/dev preload + index paths from __dirname. */
export function resolveAppPaths(dirname: string, devServerUrl: string | null): {
  preloadPath: string;
  indexHtml: string;
  iconPath: string;
} {
  return {
    preloadPath: path.join(dirname, "preload.js"),
    indexHtml: path.join(dirname, "../dist/index.html"),
    iconPath: devServerUrl
      ? path.join(dirname, "../public/icon.png")
      : path.join(dirname, "../dist/icon.png"),
  };
}
