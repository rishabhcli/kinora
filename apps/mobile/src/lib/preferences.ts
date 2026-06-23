import * as SecureStore from "expo-secure-store";
import { createStore } from "zustand/vanilla";

/**
 * User preferences — a small framework-agnostic Zustand *vanilla* store
 * (mirroring `lib/auth.ts`) persisted via expo-secure-store (Keychain on iOS,
 * Keystore on Android), the same backing store the token + onboarding flag use.
 *
 * Two booleans for now, both surfaced in the settings sheet:
 *  - `reduceMotionOverride`  — force-drop animations regardless of the OS
 *    "reduce motion" setting (honoured by `useReducedMotion`).
 *  - `autoplayOnOpen`        — start the film automatically when a book opens
 *    (honoured by the reading room).
 *
 * Values are stored as a single JSON blob under one key. Reads/writes are
 * best-effort: if secure storage is unavailable we fall back to the defaults.
 */
const PREFS_KEY = "kinora_prefs";

export interface Preferences {
  reduceMotionOverride: boolean;
  autoplayOnOpen: boolean;
}

const DEFAULTS: Preferences = {
  reduceMotionOverride: false,
  autoplayOnOpen: true,
};

export interface PreferencesState extends Preferences {
  /** True once the persisted values have been read (or failed) at boot. */
  hydrated: boolean;
  /** Replace the in-memory values (used by the boot hydrate). */
  hydrate: (prefs: Preferences) => void;
  /** Flip one preference and persist the whole blob. */
  set: <K extends keyof Preferences>(key: K, value: Preferences[K]) => void;
}

/** Coerce arbitrary parsed JSON back into a complete, typed Preferences. */
function normalise(raw: unknown): Preferences {
  const obj = (raw ?? {}) as Partial<Record<keyof Preferences, unknown>>;
  return {
    reduceMotionOverride:
      typeof obj.reduceMotionOverride === "boolean"
        ? obj.reduceMotionOverride
        : DEFAULTS.reduceMotionOverride,
    autoplayOnOpen:
      typeof obj.autoplayOnOpen === "boolean" ? obj.autoplayOnOpen : DEFAULTS.autoplayOnOpen,
  };
}

function persist(prefs: Preferences): void {
  void (async () => {
    try {
      await SecureStore.setItemAsync(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      // Best-effort; the value simply doesn't survive a relaunch.
    }
  })();
}

export const preferencesStore = createStore<PreferencesState>((set, get) => ({
  ...DEFAULTS,
  hydrated: false,
  hydrate: (prefs) => set({ ...prefs, hydrated: true }),
  set: (key, value) => {
    set({ [key]: value } as Pick<PreferencesState, typeof key>);
    const { reduceMotionOverride, autoplayOnOpen } = get();
    persist({ reduceMotionOverride, autoplayOnOpen });
  },
}));

/** Read persisted preferences from secure storage into the store (call once at boot). */
export async function loadPersistedPreferences(): Promise<void> {
  try {
    const raw = await SecureStore.getItemAsync(PREFS_KEY);
    preferencesStore.getState().hydrate(normalise(raw ? JSON.parse(raw) : null));
  } catch {
    preferencesStore.getState().hydrate({ ...DEFAULTS });
  }
}
