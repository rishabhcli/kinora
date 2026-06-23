import { contextBridge, ipcRenderer } from "electron";

/**
 * The contextIsolation bridge. `secure` proxies the safeStorage-backed token
 * store; `openBook` asks the main process to open a book in its own window.
 */
const api = {
  platform: process.platform,
  secure: {
    getToken: (): Promise<string | null> => ipcRenderer.invoke("secure:getToken"),
    setToken: (token: string | null): Promise<void> => ipcRenderer.invoke("secure:setToken", token),
  },
  openBook: (bookId: string): Promise<void> => ipcRenderer.invoke("book:open", bookId),
} as const;

export type KinoraBridge = typeof api;

if (process.contextIsolated) {
  try {
    contextBridge.exposeInMainWorld("kinora", api);
  } catch (error) {
    console.error(error);
  }
} else {
  (globalThis as unknown as { kinora: KinoraBridge }).kinora = api;
}
