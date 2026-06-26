import { app, BrowserWindow, Menu, Notification, dialog, shell, ipcMain } from "electron";
import type { MenuItemConstructorOptions } from "electron";
import path from "path";

const VITE_DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;

let mainWindow: BrowserWindow | null = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    frame: true,
    backgroundColor: "#1A1615",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  if (VITE_DEV_SERVER_URL) {
    mainWindow.loadURL(VITE_DEV_SERVER_URL);
  } else {
    mainWindow.loadFile(path.join(__dirname, "../dist/index.html"));
  }
}

/** A native macOS file picker for adding a book (PDF/EPUB). Returns the chosen
 *  path to the renderer; surfaces a native notification either way. */
async function pickBook(): Promise<string | null> {
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
}

/** Fire a native macOS notification (no-op if the OS has them disabled). */
function notify(title: string, body: string) {
  if (Notification.isSupported()) new Notification({ title, body, silent: false }).show();
}

function buildMenu() {
  const isMac = process.platform === "darwin";
  const template: MenuItemConstructorOptions[] = [
    ...(isMac
      ? [{
          label: "Kinora",
          submenu: [
            { role: "about" as const },
            { type: "separator" as const },
            { role: "hide" as const },
            { role: "hideOthers" as const },
            { role: "unhide" as const },
            { type: "separator" as const },
            { role: "quit" as const },
          ],
        }]
      : []),
    {
      label: "File",
      submenu: [
        {
          label: "Add Book…",
          accelerator: "CmdOrCtrl+O",
          click: async () => {
            const file = await pickBook();
            if (file) mainWindow?.webContents.send("kinora:add-book", file);
          },
        },
        { type: "separator" },
        isMac ? { role: "close" } : { role: "quit" },
      ],
    },
    { label: "Edit", submenu: [{ role: "undo" }, { role: "redo" }, { type: "separator" }, { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" }] },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    { role: "windowMenu" },
    {
      role: "help",
      submenu: [
        { label: "Kinora on GitHub", click: () => shell.openExternal("https://github.com/rishabhcli/kinora") },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// Let the renderer trigger the native book picker (e.g. an "Add book" button).
ipcMain.handle("kinora:pick-book", () => pickBook());
// Let the renderer raise a native notification (e.g. "your film is ready").
ipcMain.handle("kinora:notify", (_e, title: string, body: string) => {
  notify(String(title ?? "Kinora"), String(body ?? ""));
});

app.whenReady().then(() => {
  buildMenu();
  createWindow();
  notify("Kinora is ready", "Open a book to watch its AI-generated film.");
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
