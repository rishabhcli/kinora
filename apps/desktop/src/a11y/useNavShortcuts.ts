import { useEffect, useRef } from "react";
import { registerShortcut } from "./keyboard";

// Power-user navigation that is intentionally NOT surfaced anywhere in the UI
// (no cheat-sheet entries, no hints): Cmd/Ctrl+1..N jumps between the top-level
// tabs, Arrow Up/Down smoothly scrolls the shelf/content, Home/End jump to the
// ends. All registered `hidden`, and deferred whenever the reading room owns the
// keyboard or focus is inside an interactive control (so sliders/selects/inputs
// and the settings arrow-nav keep their native behavior).

export interface NavShortcutConfig {
  /** Ordered tab labels — index i binds to Cmd/Ctrl+(i+1). */
  tabs: string[];
  onSelectTab: (label: string) => void;
  /** When true, tab + arrow shortcuts are deferred (e.g. reading room open). */
  isSuppressed?: () => boolean;
}

const INTERACTIVE_SELECTOR =
  'input, textarea, select, button, [contenteditable="true"], [role="slider"], ' +
  '[role="tab"], [role="tablist"], [role="menu"], [role="menuitem"], ' +
  '[role="listbox"], [role="option"], [role="spinbutton"]';

/** True when focus sits in (or within) a control that owns arrow keys itself. */
function ownsArrows(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el?.closest) return false;
  return Boolean(el.closest(INTERACTIVE_SELECTOR));
}

function scrollingEl(): Element {
  return document.scrollingElement ?? document.documentElement;
}

export function useGlobalNavShortcuts(config: NavShortcutConfig): void {
  const ref = useRef(config);
  ref.current = config;

  useEffect(() => {
    const suppressed = () => ref.current.isSuppressed?.() ?? false;
    const unregs: Array<() => void> = [];

    ref.current.tabs.forEach((_, i) => {
      if (i > 8) return; // mod+1 .. mod+9 only
      unregs.push(
        registerShortcut(
          `mod+${i + 1}`,
          () => {
            if (suppressed()) return;
            const label = ref.current.tabs[i];
            if (label) ref.current.onSelectTab(label);
          },
          { hidden: true, preventDefault: true },
        ),
      );
    });

    const pageScroll = (dir: number, e: KeyboardEvent) => {
      if (suppressed() || ownsArrows(e.target)) return;
      e.preventDefault();
      scrollingEl().scrollBy({
        top: dir * Math.round(window.innerHeight * 0.45),
        behavior: "smooth",
      });
    };
    unregs.push(registerShortcut("arrowdown", (e) => pageScroll(1, e), { hidden: true }));
    unregs.push(registerShortcut("arrowup", (e) => pageScroll(-1, e), { hidden: true }));

    const jump = (to: "top" | "bottom", e: KeyboardEvent) => {
      if (suppressed() || ownsArrows(e.target)) return;
      e.preventDefault();
      const el = scrollingEl();
      el.scrollTo({ top: to === "top" ? 0 : el.scrollHeight, behavior: "smooth" });
    };
    unregs.push(registerShortcut("home", (e) => jump("top", e), { hidden: true }));
    unregs.push(registerShortcut("end", (e) => jump("bottom", e), { hidden: true }));

    return () => unregs.forEach((u) => u());
  }, []);
}
