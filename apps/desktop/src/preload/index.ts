import { contextBridge } from "electron";

/**
 * The contextIsolation bridge. Kept deliberately tiny for now — native
 * capabilities (local library, secure token storage, ffmpeg) land here in the
 * native-capabilities phase, each as an explicit, typed channel.
 */
const api = {
  platform: process.platform,
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
