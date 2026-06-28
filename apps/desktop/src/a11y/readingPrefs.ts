import { useState, useEffect, useCallback } from "react";

// Reader appearance + comfort, persisted globally (resume position is per-book;
// these apply to every book). Moved here from lib/ — this is reading-accessibility
// state. lib/readingPrefs.ts is a thin re-export shim for existing importers.

export type ReadingTheme = "dark" | "night" | "sepia" | "paper";
export type ReadingSpacing = "normal" | "relaxed" | "loose";
export type ReadingFontFamily = "sans" | "serif" | "dyslexic";
export type ReadingMode = "scroll" | "paged";

export interface ReadingPrefs {
  theme: ReadingTheme;
  autoNight: boolean; // force Night between 19:00 and 07:00
  fontFamily: ReadingFontFamily; // UI sans / serif / bundled dyslexia face
  fontScale: number; // multiplies the 15px base (0.8–1.6)
  leading: number; // line-height (1.3–2.4)
  measure: number; // line length in ch (44–88; ~66 is the sweet spot)
  spacing: ReadingSpacing; // letter/word spacing — a real dyslexia lever
  brightness: number; // page dim 0.5–1.0
  readingMode: ReadingMode; // continuous scroll vs. paged
  ttsRate: number; // read-aloud speed 0.5–2.0
  ttsVoiceURI: string | null; // chosen voice (null = system default)
}

export const READING_THEMES: Record<
  ReadingTheme,
  { label: string; pageBg: string; ink: string; panel: boolean; swatch: string }
> = {
  // `ink` is "r,g,b" so callers can vary alpha for active vs. dimmed text.
  dark: { label: "Dark", pageBg: "transparent", ink: "232,226,216", panel: false, swatch: "#15120e" },
  night: { label: "Night", pageBg: "rgba(0,0,0,0.55)", ink: "206,202,194", panel: true, swatch: "#000000" },
  sepia: { label: "Sepia", pageBg: "#efe6d2", ink: "62,50,33", panel: true, swatch: "#efe6d2" },
  paper: { label: "Paper", pageBg: "#f7f4ee", ink: "32,30,28", panel: true, swatch: "#f7f4ee" },
};

export const READING_SPACINGS: Record<ReadingSpacing, { label: string; letter: string; word: string }> = {
  normal: { label: "Normal", letter: "0", word: "normal" },
  relaxed: { label: "Relaxed", letter: "0.03em", word: "0.08em" },
  loose: { label: "Loose", letter: "0.06em", word: "0.16em" },
};

export const READING_FONTS: Record<
  ReadingFontFamily,
  { label: string; cssFamily: string; className: string }
> = {
  sans: { label: "Sans", cssFamily: 'var(--kinora-font-sans, "DM Sans", system-ui, sans-serif)', className: "" },
  serif: { label: "Serif", cssFamily: 'var(--kinora-font-serif, "Fraunces", Georgia, serif)', className: "" },
  dyslexic: {
    label: "Dyslexia-friendly",
    cssFamily: '"OpenDyslexic", "Atkinson Hyperlegible", "Comic Sans MS", system-ui, sans-serif',
    className: "reading-font-dyslexic",
  },
};

export const READING_BOUNDS = {
  fontScale: { min: 0.8, max: 1.6, step: 0.05 },
  leading: { min: 1.3, max: 2.4, step: 0.1 },
  measure: { min: 44, max: 88, step: 2 },
  brightness: { min: 0.5, max: 1, step: 0.05 },
  ttsRate: { min: 0.5, max: 2, step: 0.1 },
} as const;

export const DEFAULT_READING_PREFS: ReadingPrefs = {
  theme: "dark",
  autoNight: false,
  fontFamily: "sans",
  fontScale: 1,
  leading: 1.8,
  measure: 64,
  spacing: "normal",
  brightness: 1,
  readingMode: "scroll",
  ttsRate: 1,
  ttsVoiceURI: null,
};

const KEY = "kinora.readingPrefs";

/** Fired (same-tab) whenever reading prefs are persisted, so app-wide listeners
 *  (e.g. the global theme applier) can re-sync without sharing React state. */
export const READING_PREFS_EVENT = "kinora:reading-prefs-changed";

/** Read + normalize the persisted reading prefs (safe outside React). */
export function loadReadingPrefs(): ReadingPrefs {
  try {
    return normalizeReadingPrefs(JSON.parse(localStorage.getItem(KEY) || "{}") as Partial<ReadingPrefs>);
  } catch {
    return DEFAULT_READING_PREFS;
  }
}

/** Apply the effective reading theme as `html[data-theme]` so the WHOLE app
 *  (chrome + reading pane + login) re-themes, not just the text area. */
export function applyThemeAttribute(theme: ReadingTheme = resolveEffectiveTheme(loadReadingPrefs())): void {
  if (typeof document !== "undefined") document.documentElement.dataset.theme = theme;
}

export const clampPref = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

const num = (v: unknown, fallback: number) => (typeof v === "number" && Number.isFinite(v) ? v : fallback);
const isTheme = (v: unknown): v is ReadingTheme => v === "dark" || v === "night" || v === "sepia" || v === "paper";
const isSpacing = (v: unknown): v is ReadingSpacing => v === "normal" || v === "relaxed" || v === "loose";
const isFont = (v: unknown): v is ReadingFontFamily => v === "sans" || v === "serif" || v === "dyslexic";

/** Merge partial/legacy prefs onto defaults, clamping + validating every field. */
export function normalizeReadingPrefs(p: Partial<ReadingPrefs> | null | undefined): ReadingPrefs {
  const s = (p ?? {}) as Partial<ReadingPrefs>;
  const b = READING_BOUNDS;
  return {
    theme: isTheme(s.theme) ? s.theme : DEFAULT_READING_PREFS.theme,
    autoNight: typeof s.autoNight === "boolean" ? s.autoNight : DEFAULT_READING_PREFS.autoNight,
    fontFamily: isFont(s.fontFamily) ? s.fontFamily : DEFAULT_READING_PREFS.fontFamily,
    fontScale: clampPref(num(s.fontScale, DEFAULT_READING_PREFS.fontScale), b.fontScale.min, b.fontScale.max),
    leading: clampPref(num(s.leading, DEFAULT_READING_PREFS.leading), b.leading.min, b.leading.max),
    measure: clampPref(num(s.measure, DEFAULT_READING_PREFS.measure), b.measure.min, b.measure.max),
    spacing: isSpacing(s.spacing) ? s.spacing : DEFAULT_READING_PREFS.spacing,
    brightness: clampPref(num(s.brightness, DEFAULT_READING_PREFS.brightness), b.brightness.min, b.brightness.max),
    readingMode: s.readingMode === "paged" ? "paged" : "scroll",
    ttsRate: clampPref(num(s.ttsRate, DEFAULT_READING_PREFS.ttsRate), b.ttsRate.min, b.ttsRate.max),
    ttsVoiceURI: typeof s.ttsVoiceURI === "string" ? s.ttsVoiceURI : null,
  };
}

/** Resolve the theme to actually render (honours autoNight). `hour` is injectable for tests. */
export function resolveEffectiveTheme(prefs: ReadingPrefs, hour: number = new Date().getHours()): ReadingTheme {
  return prefs.autoNight && (hour >= 19 || hour < 7) ? "night" : prefs.theme;
}

export function useReadingPrefs() {
  const [prefs, setPrefs] = useState<ReadingPrefs>(loadReadingPrefs);

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(prefs));
    } catch {
      /* storage blocked */
    }
    // Notify app-wide listeners (the global theme applier) in the same tab.
    if (typeof window !== "undefined") window.dispatchEvent(new Event(READING_PREFS_EVENT));
  }, [prefs]);

  const update = useCallback((p: Partial<ReadingPrefs>) => {
    setPrefs((cur) => normalizeReadingPrefs({ ...cur, ...p }));
  }, []);

  const effectiveTheme = resolveEffectiveTheme(prefs);

  return { prefs, update, effectiveTheme };
}
