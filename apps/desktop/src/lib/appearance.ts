// Applies the Appearance settings (reduce motion / reduce transparency / increase
// contrast) app-wide, WITHOUT editing Agent 8's index.css: we toggle classes on
// <html> and maintain a single managed <style> element. The CSS lives here (our
// lane) and overrides via `!important` + specificity.
//
// Reach: kept for tests/imperative consumers. The mounted A11yProvider is the
// runtime owner of <html> classes so Settings changes apply from first paint.
import { resolveAppearance, settingsStore, type AppSettings } from "./settings.ts";

const STYLE_ID = "kinora-settings-overrides";

const APPEARANCE_QUERIES = {
  reduceMotion: "(prefers-reduced-motion: reduce)",
  reduceTransparency: "(prefers-reduced-transparency: reduce)",
  increaseContrast: "(prefers-contrast: more)",
} as const;

export interface ResolvedAppearance {
  reduceMotion: boolean;
  reduceTransparency: boolean;
  increaseContrast: boolean;
}

function systemPrefers(query: string): boolean {
  try {
    return typeof matchMedia !== "undefined" && matchMedia(query).matches;
  } catch {
    return false;
  }
}

export function resolveAppearanceSettings(s: AppSettings = settingsStore.get()): ResolvedAppearance {
  return {
    reduceMotion: resolveAppearance(s.reduceMotion, systemPrefers(APPEARANCE_QUERIES.reduceMotion)),
    reduceTransparency: resolveAppearance(
      s.reduceTransparency,
      systemPrefers(APPEARANCE_QUERIES.reduceTransparency),
    ),
    increaseContrast: resolveAppearance(s.increaseContrast, systemPrefers(APPEARANCE_QUERIES.increaseContrast)),
  };
}

/** The injected override stylesheet. Pure string so it's unit-testable. */
export function overrideCss(): string {
  return `
/* reduce-motion: neutralise CSS animation/transition (framer JS springs honour
   useReducedMotion separately). */
html.kinora-reduce-motion *,
html.kinora-reduce-motion *::before,
html.kinora-reduce-motion *::after {
  animation-duration: .001ms !important;
  animation-iteration-count: 1 !important;
  transition-duration: .001ms !important;
  scroll-behavior: auto !important;
}
/* reduce-transparency: drop the glass blur (esp. the native-shell vibrancy skin). */
html.kinora-reduce-transparency [class*="backdrop"],
html.kinora-reduce-transparency .glass-input,
html.kinora-reduce-transparency .liquid-glass-dock,
html.kinora-reduce-transparency header,
html.kinora-reduce-transparency .footer-glass,
html.kinora-native.kinora-reduce-transparency button,
html.kinora-native.kinora-reduce-transparency input,
html.kinora-native.kinora-reduce-transparency [class*="rounded"] {
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
}
/* increase-contrast: brighten secondary text + firm up hairline borders. */
html.kinora-increase-contrast .text-kinora-muted { color: #d6cdc1 !important; }
html.kinora-increase-contrast .text-kinora-subtle { color: #b7aea3 !important; }
html.kinora-increase-contrast [class*="border-white/"] { border-color: rgba(255,255,255,.28) !important; }
`;
}

let styleEl: HTMLStyleElement | null = null;

export function applyAppearanceEffects(): void {
  if (typeof document === "undefined") return;
  const r = resolveAppearanceSettings();
  const root = document.documentElement;
  root.classList.toggle("kinora-reduce-motion", r.reduceMotion);
  root.classList.toggle("kinora-reduce-transparency", r.reduceTransparency);
  root.classList.toggle("kinora-increase-contrast", r.increaseContrast);

  if (!styleEl) {
    styleEl = document.getElementById(STYLE_ID) as HTMLStyleElement | null;
  }
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = STYLE_ID;
    document.head.appendChild(styleEl);
  }
  if (styleEl.textContent === "") styleEl.textContent = overrideCss();
}

let stop: (() => void) | null = null;

/** Start applying + keep in sync with the store and OS preference changes. Idempotent. */
export function startAppearanceSync(): () => void {
  if (typeof window === "undefined") return () => {};
  if (stop) return stop;
  applyAppearanceEffects();
  const unsub = settingsStore.subscribe(applyAppearanceEffects);
  const onChange = () => applyAppearanceEffects();
  const mqls = Object.values(APPEARANCE_QUERIES)
    .map((q) => {
      try {
        return matchMedia(q);
      } catch {
        return null;
      }
    })
    .filter((m): m is MediaQueryList => m != null);
  mqls.forEach((m) => m.addEventListener("change", onChange));
  stop = () => {
    unsub();
    mqls.forEach((m) => m.removeEventListener("change", onChange));
    stop = null;
  };
  return stop;
}
