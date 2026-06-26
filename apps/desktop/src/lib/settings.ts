// Kinora app settings — a small, persisted, observable store.
//
// Scope: this owns the *non-reading* preferences. Reading prefs (theme, font
// scale, leading, measure, spacing, auto-night) belong to Agent 6's
// `useReadingPrefs` (key `kinora.readingPrefs`) and are NOT duplicated here — the
// Settings UI composes that hook so the reading room and Settings share one truth.
//
// No React in this file: the store is framework-agnostic (get/set/subscribe), so
// it's unit-testable under `node --test` and consumed by the `useSettings` hook
// via `useSyncExternalStore`. Structured for a future backend sync (one JSON blob
// under one key; `diffFromDefaults` is the natural sync payload).

export type SystemOverride = "system" | "on" | "off";
export type LaunchView = "Home" | "Library" | "Watch" | "Favorites" | "Notes";

export interface AppSettings {
  // General
  launchView: LaunchView;
  // Appearance — override the OS accessibility prefs, applied app-wide
  reduceMotion: SystemOverride;
  reduceTransparency: SystemOverride;
  increaseContrast: SystemOverride;
  // Playback / Film
  autoplayFilm: boolean;
  captions: boolean;
  scrubSensitivity: number; // 0.5 (precise) – 2.0 (fast)
  // Notifications
  notificationsEnabled: boolean;
  readingReminders: boolean;
  weeklyDigest: boolean;
  soundEffects: boolean;
  // Privacy
  analytics: boolean;
  crashReports: boolean;
}

export const SETTINGS_DEFAULTS: AppSettings = {
  launchView: "Home",
  reduceMotion: "system",
  reduceTransparency: "system",
  increaseContrast: "system",
  autoplayFilm: true,
  captions: false,
  scrubSensitivity: 1,
  notificationsEnabled: false,
  readingReminders: false,
  weeklyDigest: false,
  soundEffects: false,
  analytics: false,
  crashReports: true,
};

export const SETTINGS_KEY = "kinora.settings";
export const SCRUB_RANGE = { min: 0.5, max: 2 } as const;

const LAUNCH_VIEWS: readonly LaunchView[] = ["Home", "Library", "Watch", "Favorites", "Notes"];
const OVERRIDES: readonly SystemOverride[] = ["system", "on", "off"];

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

/** Coerce arbitrary stored/incoming data into a valid, fully-populated AppSettings. */
export function mergeSettings(raw: unknown): AppSettings {
  const out: AppSettings = { ...SETTINGS_DEFAULTS };
  if (!raw || typeof raw !== "object") return out;
  const r = raw as Record<string, unknown>;

  const bool = (k: keyof AppSettings) => {
    if (typeof r[k] === "boolean") (out[k] as boolean) = r[k] as boolean;
  };
  bool("autoplayFilm");
  bool("captions");
  bool("notificationsEnabled");
  bool("readingReminders");
  bool("weeklyDigest");
  bool("soundEffects");
  bool("analytics");
  bool("crashReports");

  if (typeof r.scrubSensitivity === "number" && Number.isFinite(r.scrubSensitivity)) {
    out.scrubSensitivity = clamp(r.scrubSensitivity, SCRUB_RANGE.min, SCRUB_RANGE.max);
  }
  if (typeof r.launchView === "string" && LAUNCH_VIEWS.includes(r.launchView as LaunchView)) {
    out.launchView = r.launchView as LaunchView;
  }
  for (const k of ["reduceMotion", "reduceTransparency", "increaseContrast"] as const) {
    if (typeof r[k] === "string" && OVERRIDES.includes(r[k] as SystemOverride)) {
      out[k] = r[k] as SystemOverride;
    }
  }
  return out;
}

/** Resolve a tri-state override against the live OS preference. */
export function resolveAppearance(value: SystemOverride, systemPrefers: boolean): boolean {
  return value === "system" ? systemPrefers : value === "on";
}

/** The keys whose value differs from the shipped default (the sync payload / "what changed"). */
export function diffFromDefaults(settings: AppSettings): Partial<AppSettings> {
  const diff: Partial<AppSettings> = {};
  for (const k of Object.keys(SETTINGS_DEFAULTS) as (keyof AppSettings)[]) {
    if (settings[k] !== SETTINGS_DEFAULTS[k]) (diff[k] as AppSettings[typeof k]) = settings[k];
  }
  return diff;
}

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** localStorage when present (renderer); a throwaway in-memory map otherwise (SSR/tests). */
function defaultBacking(): KeyValueStore {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch {
    /* storage blocked */
  }
  const m = new Map<string, string>();
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
}

export interface SettingsStore {
  /** Current snapshot — referentially stable until a change (useSyncExternalStore-safe). */
  get(): AppSettings;
  /** Merge a partial patch, persist, and notify. */
  set(patch: Partial<AppSettings>): void;
  /** Reset everything to defaults. */
  reset(): void;
  /** Reset a single field to its default. */
  resetKey(key: keyof AppSettings): void;
  subscribe(listener: () => void): () => void;
}

export function createSettingsStore(backing: KeyValueStore | null = defaultBacking()): SettingsStore {
  const store: KeyValueStore = backing ?? defaultBacking();
  let state: AppSettings = load();
  const listeners = new Set<() => void>();

  function load(): AppSettings {
    try {
      return mergeSettings(JSON.parse(store.getItem(SETTINGS_KEY) || "null"));
    } catch {
      return { ...SETTINGS_DEFAULTS };
    }
  }
  function commit(next: AppSettings) {
    state = next;
    try {
      store.setItem(SETTINGS_KEY, JSON.stringify(next));
    } catch {
      /* storage blocked — keep in memory */
    }
    listeners.forEach((l) => l());
  }

  return {
    get: () => state,
    set: (patch) => commit(mergeSettings({ ...state, ...patch })),
    reset: () => commit({ ...SETTINGS_DEFAULTS }),
    resetKey: (key) => commit(mergeSettings({ ...state, [key]: SETTINGS_DEFAULTS[key] })),
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** The app-wide singleton (renderer uses localStorage). */
export const settingsStore = createSettingsStore();
