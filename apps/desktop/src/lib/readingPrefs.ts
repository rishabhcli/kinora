import { useState, useEffect } from "react";

// Reader appearance + comfort, persisted globally (the resume position is
// per-book; these preferences apply to every book).

export type ReadingTheme = "dark" | "night" | "sepia" | "paper";
export type ReadingSpacing = "normal" | "relaxed" | "loose";

export interface ReadingPrefs {
  theme: ReadingTheme;
  autoNight: boolean; // force Night between 19:00 and 07:00
  fontScale: number; // multiplies the 15px base (0.85–1.5)
  leading: number; // line-height (1.4–2.2)
  measure: number; // line length in ch (48–80; ~66 is the readability sweet spot)
  spacing: ReadingSpacing; // letter/word spacing — the real dyslexia lever (not special fonts)
}

export const READING_THEMES: Record<
  ReadingTheme,
  { label: string; pageBg: string; ink: string; panel: boolean; swatch: string }
> = {
  // `ink` is "r,g,b" so callers can vary alpha for active vs dimmed text.
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

const DEFAULTS: ReadingPrefs = {
  theme: "dark",
  autoNight: false,
  fontScale: 1,
  leading: 1.8,
  measure: 64,
  spacing: "normal",
};

const KEY = "kinora.readingPrefs";
export const clampPref = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

export function useReadingPrefs() {
  const [prefs, setPrefs] = useState<ReadingPrefs>(() => {
    try {
      return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY) || "{}") as Partial<ReadingPrefs>) };
    } catch {
      return DEFAULTS;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(prefs));
    } catch {
      /* storage blocked */
    }
  }, [prefs]);

  const update = (p: Partial<ReadingPrefs>) => setPrefs((cur) => ({ ...cur, ...p }));

  const hour = new Date().getHours();
  const effectiveTheme: ReadingTheme =
    prefs.autoNight && (hour >= 19 || hour < 7) ? "night" : prefs.theme;

  return { prefs, update, effectiveTheme };
}
