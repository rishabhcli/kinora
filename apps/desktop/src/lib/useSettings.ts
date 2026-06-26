import { useSyncExternalStore } from "react";
import { settingsStore, type AppSettings } from "./settings";

export interface UseSettings {
  settings: AppSettings;
  set: (patch: Partial<AppSettings>) => void;
  reset: () => void;
  resetKey: (key: keyof AppSettings) => void;
}

/** Subscribe to the app settings store. The snapshot is referentially stable
 *  between changes, so this is safe for `useSyncExternalStore`. */
export function useSettings(): UseSettings {
  const settings = useSyncExternalStore(settingsStore.subscribe, settingsStore.get, settingsStore.get);
  return {
    settings,
    set: settingsStore.set,
    reset: settingsStore.reset,
    resetKey: settingsStore.resetKey,
  };
}
