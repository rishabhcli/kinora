import { useStore } from "zustand";

import { type PreferencesState, preferencesStore } from "../lib/preferences";

/** Bind the framework-agnostic vanilla preferences store into React (mirrors useAuth). */
export function usePreferences<T>(selector: (state: PreferencesState) => T): T {
  return useStore(preferencesStore, selector);
}
