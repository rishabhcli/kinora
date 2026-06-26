import { useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { useReducedMotionPref } from "./useReducedMotionPref";
import { useHighContrastPref, useReducedTransparencyPref } from "./displayPrefs";
import { registerShortcut } from "./keyboard";
import { ShortcutCheatSheet } from "./ShortcutCheatSheet";
import { settingsStore } from "../lib/settings";

// Mounted once (wrapping <App/> in main.tsx) so it also covers the login screen.
// Reflects the three a11y display preferences onto <html> for a11y.css, hosts the
// global `?` cheat-sheet, and provides the skip-to-content link.

function useHtmlClass(cls: string, on: boolean): void {
  useEffect(() => {
    document.documentElement.classList.toggle(cls, on);
  }, [cls, on]);
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
