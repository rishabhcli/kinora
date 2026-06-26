// Focus management for dialogs/popovers: enumerate focusables, trap Tab inside
// a container, and restore focus to whatever opened it. jsdom has no layout, so
// visibility is judged by attributes + inline style (sufficient for the app's
// inline-styled overlays and unit tests).

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "area[href]",
  "button",
  "input",
  "select",
  "textarea",
  "iframe",
  "audio[controls]",
  "video[controls]",
  '[contenteditable]:not([contenteditable="false"])',
  "[tabindex]",
].join(",");

export function getFocusable(container: HTMLElement): HTMLElement[] {
  const els = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
  return els.filter((el) => {
    if (el.hasAttribute("disabled")) return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    if (el.tabIndex < 0) return false;
    if ((el as HTMLInputElement).type === "hidden") return false;
    if (el.hidden) return false;
    if (el.style.display === "none" || el.style.visibility === "hidden") return false;
    return true;
  });
}

/** Trap Tab/Shift+Tab within `container`. Returns a release() function. */
export function trapFocus(container: HTMLElement): () => void {
  function onKeydown(e: KeyboardEvent): void {
    if (e.key !== "Tab") return;
    const focusable = getFocusable(container);
    if (focusable.length === 0) {
      e.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (!container.contains(active)) {
      e.preventDefault();
      first.focus();
    } else if (e.shiftKey && active === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }
  container.addEventListener("keydown", onKeydown);
  return () => container.removeEventListener("keydown", onKeydown);
}

/** Return focus to a previously-focused element (e.g. on dialog close). */
export function restoreFocus(previouslyFocused: HTMLElement | null): void {
  if (previouslyFocused && typeof previouslyFocused.focus === "function") {
    previouslyFocused.focus();
  }
}
