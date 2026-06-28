import { useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { useReducedMotionPref } from "./useReducedMotionPref";
import { useHighContrastPref, useReducedTransparencyPref } from "./displayPrefs";
import { registerShortcut } from "./keyboard";
import { ShortcutCheatSheet } from "./ShortcutCheatSheet";
import { settingsStore } from "../lib/settings";
import {
  READING_PREFS_EVENT,
  applyThemeAttribute,
  loadReadingPrefs,
  resolveEffectiveTheme,
} from "./readingPrefs";

// Mounted once (wrapping <App/> in main.tsx) so it also covers the login screen.
// Reflects the three a11y display preferences onto <html> for a11y.css, hosts the
// global `?` cheat-sheet, and provides the skip-to-content link.

function useHtmlClass(cls: string, on: boolean): void {
  useEffect(() => {
    document.documentElement.classList.toggle(cls, on);
  }, [cls, on]);
}

/** Reflect the reader's chosen theme onto `html[data-theme]` so the ENTIRE app
 *  (chrome, cards, login, reading pane) re-themes — driven by tokens.css. Stays
 *  in sync across components via the reading-prefs change event + cross-tab
 *  storage, and re-resolves on a slow timer so autoNight flips at dusk/dawn. */
function useGlobalReadingTheme(): void {
  const [theme, setTheme] = useState(() => resolveEffectiveTheme(loadReadingPrefs()));
  useEffect(() => {
    const sync = () => setTheme(resolveEffectiveTheme(loadReadingPrefs()));
    sync();
    window.addEventListener(READING_PREFS_EVENT, sync);
    window.addEventListener("storage", sync);
    const timer = window.setInterval(sync, 60_000); // autoNight boundary
    return () => {
      window.removeEventListener(READING_PREFS_EVENT, sync);
      window.removeEventListener("storage", sync);
      window.clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    applyThemeAttribute(theme);
  }, [theme]);
}

function SkipLink() {
  return (
    <a
      className="skip-link"
      href="#kinora-main"
      onClick={(e) => {
        const main =
          document.getElementById("kinora-main") ??
          document.querySelector("main") ??
          document.querySelector('[role="main"]');
        if (main) {
          e.preventDefault();
          const el = main as HTMLElement;
          if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
          el.focus();
        }
      }}
    >
      Skip to content
    </a>
  );
}

export interface A11yProviderProps {
  children: ReactNode;
}

export function A11yProvider({ children }: A11yProviderProps) {
  const settings = useSyncExternalStore(
    settingsStore.subscribe,
    settingsStore.get,
    settingsStore.get,
  );
  const mediaReduceMotion = useReducedMotionPref();
  const mediaHighContrast = useHighContrastPref();
  const mediaReduceTransparency = useReducedTransparencyPref();
  const reduceMotion =
    settings.reduceMotion === "system" ? mediaReduceMotion : settings.reduceMotion === "on";
  const highContrast =
    settings.increaseContrast === "system" ? mediaHighContrast : settings.increaseContrast === "on";
  const reduceTransparency =
    settings.reduceTransparency === "system"
      ? mediaReduceTransparency
      : settings.reduceTransparency === "on";
  useHtmlClass("kinora-reduce-motion", reduceMotion);
  useHtmlClass("kinora-high-contrast", highContrast);
  useHtmlClass("kinora-increase-contrast", highContrast);
  useHtmlClass("kinora-reduce-transparency", reduceTransparency);
  useGlobalReadingTheme();

  const [sheetOpen, setSheetOpen] = useState(false);
  useEffect(
    () =>
      registerShortcut("?", () => setSheetOpen((o) => !o), {
        description: "Show this shortcut list",
        scope: "Global",
        preventDefault: true,
      }),
    [],
  );

  return (
    <>
      <SkipLink />
      {children}
      <ShortcutCheatSheet open={sheetOpen} onClose={() => setSheetOpen(false)} />
    </>
  );
}
