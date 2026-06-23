import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { app, BrowserWindow, ipcMain, safeStorage } from "electron";

function tokenFile(): string {
  return join(app.getPath("userData"), "session.bin");
}

/** Encrypted token storage backed by the OS keychain via Electron safeStorage. */
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
      // Persistence is best-effort; a failure just means the user re-logs in.
    }
  });
}

function createWindow(): void {
  const window = new BrowserWindow({
    width: 1280,
    height: 860,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: "#0a0a0a",
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      contextIsolation: true,
      sandbox: false,
    },
  });

  window.on("ready-to-show", () => window.show());

  const devUrl = process.env["ELECTRON_RENDERER_URL"];
  if (devUrl) {
    void window.loadURL(devUrl);
  } else {
    void window.loadFile(join(__dirname, "../renderer/index.html"));
  }
}

void app.whenReady().then(() => {
  registerSecureStorage();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
