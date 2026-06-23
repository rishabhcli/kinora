import { contextBridge, ipcRenderer } from "electron";

/**
 * The contextIsolation bridge. `secure` proxies the main process's
 * safeStorage-backed token store; more native channels (local library, ffmpeg)
 * land here in later increments.
 */
const api = {
  platform: process.platform,
  secure: {
    getToken: (): Promise<string | null> => ipcRenderer.invoke("secure:getToken"),
    setToken: (token: string | null): Promise<void> => ipcRenderer.invoke("secure:setToken", token),
  },
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
