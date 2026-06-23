/**
 * The reading-room theme system (Apple Books-style "Themes & Settings").
 *
 * A theme is a small set of CSS custom properties applied to the reading column
 * — paper background, ink, accent, and the page chrome — so selecting one
 * live-restyles the pane without re-rendering content. Customize options (size,
 * family, leading, brightness) layer on top. Both halves persist to
 * localStorage so the reader returns to the same page-feel.
 *
 * Framework-light: a tiny external store + a React hook, mirroring the way the
 * SyncEngine is consumed, so any reading-room component can read/update it.
 */
import { useSyncExternalStore } from "react";

export type ReadingThemeId = "original" | "quiet" | "paper" | "bold" | "calm" | "focus";

export interface ReadingTheme {
  id: ReadingThemeId;
  /** Display name shown in the popover. */
  label: string;
  /** Whether the page surface reads as a light or dark leaf (drives chrome). */
  tone: "light" | "dark";
  /** CSS custom-property values applied to the reading column. */
  vars: {
    "--page-bg": string;
    /** A second stop so the leaf has a faint, paper-like vertical wash. */
    "--page-bg-edge": string;
    "--page-ink": string;
    "--page-ink-soft": string;
    "--page-accent": string;
    /** The karaoke wash painted under the spoken word. */
    "--page-highlight": string;
    /** The leaf's edge line + drop shadow tint. */
    "--page-rule": string;
    "--page-shadow": string;
  };
  /** A two-stop swatch used to preview the theme in the popover. */
  swatch: [string, string];
}

export const READING_THEMES: ReadingTheme[] = [
  {
    id: "original",
    label: "Original",
    tone: "light",
    vars: {
      "--page-bg": "#f3ecdd",
      "--page-bg-edge": "#ece2cd",
      "--page-ink": "#241811",
      "--page-ink-soft": "#6f5c49",
      "--page-accent": "#c26a24",
      "--page-highlight": "rgba(224, 134, 58, 0.26)",
      "--page-rule": "rgba(36, 24, 17, 0.10)",
      "--page-shadow": "rgba(22, 14, 8, 0.34)",
    },
    swatch: ["#f3ecdd", "#241811"],
  },
  {
    id: "quiet",
    label: "Quiet",
    tone: "light",
    vars: {
      "--page-bg": "#ffffff",
      "--page-bg-edge": "#f4f4f3",
      "--page-ink": "#1d1d20",
      "--page-ink-soft": "#6b6b72",
      "--page-accent": "#8a6a3f",
      "--page-highlight": "rgba(20, 20, 22, 0.08)",
      "--page-rule": "rgba(0, 0, 0, 0.07)",
      "--page-shadow": "rgba(0, 0, 0, 0.22)",
    },
    swatch: ["#ffffff", "#1d1d20"],
  },
  {
    id: "paper",
    label: "Paper",
    tone: "light",
    vars: {
      "--page-bg": "#e7dcc4",
      "--page-bg-edge": "#ddd0b2",
      "--page-ink": "#322517",
      "--page-ink-soft": "#73624a",
      "--page-accent": "#9a6a2c",
      "--page-highlight": "rgba(154, 106, 44, 0.24)",
      "--page-rule": "rgba(50, 37, 23, 0.12)",
      "--page-shadow": "rgba(40, 28, 14, 0.3)",
    },
    swatch: ["#e7dcc4", "#322517"],
  },
  {
    id: "bold",
    label: "Bold",
    tone: "light",
    vars: {
      "--page-bg": "#fbfbfb",
      "--page-bg-edge": "#f0f0f0",
      "--page-ink": "#000000",
      "--page-ink-soft": "#3a3a3a",
      "--page-accent": "#b4521c",
      "--page-highlight": "rgba(0, 0, 0, 0.12)",
      "--page-rule": "rgba(0, 0, 0, 0.14)",
      "--page-shadow": "rgba(0, 0, 0, 0.28)",
    },
    swatch: ["#fbfbfb", "#000000"],
  },
  {
    id: "calm",
    label: "Calm",
    tone: "dark",
    vars: {
      "--page-bg": "#2b3640",
      "--page-bg-edge": "#222c35",
      "--page-ink": "#e7eef3",
      "--page-ink-soft": "#9fb1bd",
      "--page-accent": "#7bc0d8",
      "--page-highlight": "rgba(123, 192, 216, 0.22)",
      "--page-rule": "rgba(231, 238, 243, 0.12)",
      "--page-shadow": "rgba(0, 0, 0, 0.5)",
    },
    swatch: ["#2b3640", "#e7eef3"],
  },
  {
    id: "focus",
    label: "Focus",
    tone: "dark",
    vars: {
      "--page-bg": "#16100b",
      "--page-bg-edge": "#0f0a06",
      "--page-ink": "#efe6d6",
      "--page-ink-soft": "#a08d77",
      "--page-accent": "#f4a85d",
      "--page-highlight": "rgba(244, 168, 93, 0.22)",
      "--page-rule": "rgba(239, 230, 214, 0.1)",
      "--page-shadow": "rgba(0, 0, 0, 0.6)",
    },
    swatch: ["#16100b", "#efe6d6"],
  },
];

export const FONT_FAMILIES = [
  { id: "serif", label: "New York", stack: 'ui-serif, "New York", Georgia, "Times New Roman", serif' },
  { id: "sans", label: "San Francisco", stack: '-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif' },
  { id: "literata", label: "Literata", stack: 'Literata, Charter, Georgia, "Times New Roman", serif' },
  { id: "mono", label: "Mono", stack: 'ui-monospace, "SF Mono", Menlo, monospace' },
] as const;

export type FontFamilyId = (typeof FONT_FAMILIES)[number]["id"];

export interface ReadingSettings {
  themeId: ReadingThemeId;
  /** Body font size in px (the leaf scales its measure with it). */
  fontSize: number;
  fontFamily: FontFamilyId;
  /** Multiplier on line-height. */
  lineSpacing: number;
  /** 0.7–1.0 — dims the leaf for low-light reading. */
  brightness: number;
}

export const FONT_SIZE_MIN = 15;
export const FONT_SIZE_MAX = 28;
export const LINE_SPACING_MIN = 1.4;
export const LINE_SPACING_MAX = 2.1;

const DEFAULT_SETTINGS: ReadingSettings = {
  themeId: "original",
  fontSize: 19,
  fontFamily: "serif",
  lineSpacing: 1.7,
  brightness: 1,
};

const STORAGE_KEY = "kinora.reading.settings.v1";

export function themeById(id: ReadingThemeId): ReadingTheme {
  return READING_THEMES.find((t) => t.id === id) ?? READING_THEMES[0]!;
}

export function fontStack(id: FontFamilyId): string {
  return (FONT_FAMILIES.find((f) => f.id === id) ?? FONT_FAMILIES[0]).stack;
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value));
}

function sanitize(raw: Partial<ReadingSettings> | null | undefined): ReadingSettings {
  if (!raw) return DEFAULT_SETTINGS;
  const themeId = READING_THEMES.some((t) => t.id === raw.themeId)
    ? (raw.themeId as ReadingThemeId)
    : DEFAULT_SETTINGS.themeId;
  const fontFamily = FONT_FAMILIES.some((f) => f.id === raw.fontFamily)
    ? (raw.fontFamily as FontFamilyId)
    : DEFAULT_SETTINGS.fontFamily;
  return {
    themeId,
    fontFamily,
    fontSize: clamp(Number(raw.fontSize) || DEFAULT_SETTINGS.fontSize, FONT_SIZE_MIN, FONT_SIZE_MAX),
    lineSpacing: clamp(
      Number(raw.lineSpacing) || DEFAULT_SETTINGS.lineSpacing,
      LINE_SPACING_MIN,
      LINE_SPACING_MAX,
    ),
    brightness: clamp(Number(raw.brightness) || DEFAULT_SETTINGS.brightness, 0.7, 1),
  };
}

function load(): ReadingSettings {
  if (typeof localStorage === "undefined") return DEFAULT_SETTINGS;
  try {
    return sanitize(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null"));
  } catch {
    return DEFAULT_SETTINGS;
  }
}

// --- tiny external store (useSyncExternalStore) ---------------------------- #

let current = load();
const listeners = new Set<() => void>();

function persist(): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    /* private mode / quota — keep the in-memory value */
  }
}

function set(patch: Partial<ReadingSettings>): void {
  current = sanitize({ ...current, ...patch });
  persist();
  for (const listener of listeners) listener();
}

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};

const getSnapshot = (): ReadingSettings => current;

export interface UseReadingThemeResult {
  settings: ReadingSettings;
  theme: ReadingTheme;
  setTheme: (id: ReadingThemeId) => void;
  setFontSize: (px: number) => void;
  setFontFamily: (id: FontFamilyId) => void;
  setLineSpacing: (value: number) => void;
  setBrightness: (value: number) => void;
}

/** Read + mutate the persisted reading settings from any reading-room component. */
export function useReadingTheme(): UseReadingThemeResult {
  const settings = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return {
    settings,
    theme: themeById(settings.themeId),
    setTheme: (id) => set({ themeId: id }),
    setFontSize: (px) => set({ fontSize: px }),
    setFontFamily: (id) => set({ fontFamily: id }),
    setLineSpacing: (value) => set({ lineSpacing: value }),
    setBrightness: (value) => set({ brightness: value }),
  };
}
