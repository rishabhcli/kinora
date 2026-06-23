import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

import { app, BrowserWindow, ipcMain, safeStorage } from "electron";

const require = createRequire(import.meta.url);

function tokenFile(): string {
  return join(app.getPath("userData"), "session.bin");
}

function registerSecureStorage(): void {
  ipcMain.handle("secure:getToken", (): string | null => {
    try {
      const file = tokenFile();
      if (!existsSync(file) || !safeStorage.isEncryptionAvailable()) return null;
      return safeStorage.decryptString(readFileSync(file));
    } catch {
      return null;
    }
  });
  ipcMain.handle("secure:setToken", (_event, token: string | null): void => {
    try {
      const file = tokenFile();
      if (!token) {
        if (existsSync(file)) rmSync(file);
        return;
      }
      if (safeStorage.isEncryptionAvailable()) {
        writeFileSync(file, safeStorage.encryptString(token));
      }
    } catch {
      // best-effort
    }
  });
}

function preloadPath(): string {
  return join(__dirname, "../preload/index.js");
}

function loadRoute(window: BrowserWindow, route: string): void {
  const devUrl = process.env["ELECTRON_RENDERER_URL"];
  if (devUrl) {
    void window.loadURL(`${devUrl}/#${route}`);
  } else {
    void window.loadFile(join(__dirname, "../renderer/index.html"), { hash: route });
  }
}

/** Apply real macOS Liquid Glass (NSGlassEffectView) behind the web content.
 *  Requires a transparent window (no vibrancy) and must run after load. */
function applyLiquidGlass(window: BrowserWindow): void {
  window.webContents.once("did-finish-load", () => {
    try {
      const liquidGlass = require("electron-liquid-glass") as {
        isGlassSupported: () => boolean;
        addView: (handle: Buffer, opts: { cornerRadius: number }) => number;
      };
      const supported = liquidGlass.isGlassSupported();
      const viewId = liquidGlass.addView(window.getNativeWindowHandle(), { cornerRadius: 18 });
      console.log(`[kinora] liquid-glass attached: supported=${supported} viewId=${viewId}`);
    } catch {
      // Native module is macOS-only; Linux/Windows builds skip gracefully.
      console.log("[kinora] liquid-glass: unavailable on this platform");
    }
  });
}

function createWindow(): void {
  const window = new BrowserWindow({
    width: 1320,
    height: 880,
    minWidth: 940,
    minHeight: 640,
    show: false,
    titleBarStyle: "hidden",
    trafficLightPosition: { x: 18, y: 24 },
    transparent: true,
    webPreferences: { preload: preloadPath(), contextIsolation: true, sandbox: false },
  });
  window.on("ready-to-show", () => window.show());
  applyLiquidGlass(window);
  loadRoute(window, "/");
}

/** A book opens in its own dedicated reading window (Apple Books style). */
function createBookWindow(bookId: string): void {
  const window = new BrowserWindow({
    width: 1120,
    height: 840,
    minWidth: 720,
    minHeight: 560,
    show: false,
    titleBarStyle: "hidden",
    trafficLightPosition: { x: 18, y: 22 },
    transparent: true,
    webPreferences: { preload: preloadPath(), contextIsolation: true, sandbox: false },
  });
  window.on("ready-to-show", () => window.show());
  applyLiquidGlass(window);
  loadRoute(window, `/book/${bookId}`);
}

void app.whenReady().then(() => {
  registerSecureStorage();
  ipcMain.handle("book:open", (_event, bookId: unknown) => {
    if (typeof bookId === "string" && bookId.length > 0) createBookWindow(bookId);
  });
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
