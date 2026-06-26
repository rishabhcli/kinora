// Preload — runs in the isolated context with node access. We expose a single
// flag telling the renderer that the window is backed by a *native* OS glass
// material (macOS vibrancy / Windows 11 acrylic), so the web UI can go
// translucent and let that real glass show through. On Linux there's no native
// material, so the flag stays off and the UI keeps its solid background.
import { contextBridge } from "electron";

const hasNativeGlass = process.platform === "darwin" || process.platform === "win32";

if (hasNativeGlass) {
  // Reuse the same flag the native Swift shell sets, so one CSS path
  // (`html.kinora-native`) drives translucency in every native host.
  contextBridge.exposeInMainWorld("__KINORA_NATIVE__", true);
}
