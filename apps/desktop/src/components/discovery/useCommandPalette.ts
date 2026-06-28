// useCommandPalette — owns the ⌘K (and Ctrl+K) global shortcut + open/close
// state for the command palette. Registers through the a11y keyboard registry so
// it composes with the rest of the app's shortcuts and is suppressible (e.g.
// while the reading room owns the keyboard).
import { useCallback, useEffect, useState } from "react";
import { registerShortcut } from "../../a11y/keyboard";
import { announcePaletteOpen } from "./CommandPalette";

export interface UseCommandPaletteOptions {
  /** Suppress the shortcut (e.g. while a book is open). */
  isSuppressed?: () => boolean;
  /** Also bind "/" to open (common quick-search affordance). Default true. */
  bindSlash?: boolean;
}

export interface CommandPaletteController {
  open: boolean;
  show: () => void;
  hide: () => void;
  toggle: () => void;
}

export function useCommandPalette(opts: UseCommandPaletteOptions = {}): CommandPaletteController {
  const [open, setOpen] = useState(false);
  const bindSlash = opts.bindSlash ?? true;

  const show = useCallback(() => {
    setOpen(true);
    announcePaletteOpen();
  }, []);
  const hide = useCallback(() => setOpen(false), []);
  const toggle = useCallback(() => setOpen((o) => !o), []);

  useEffect(() => {
    const suppressed = () => opts.isSuppressed?.() ?? false;
    const unregs: (() => void)[] = [];

    unregs.push(
      registerShortcut(
        "mod+k",
        (e) => {
          if (suppressed()) return;
          e.preventDefault();
          setOpen((o) => {
            if (!o) announcePaletteOpen();
            return !o;
          });
        },
        { preventDefault: true, description: "Open the command palette" },
      ),
    );

    if (bindSlash) {
      unregs.push(
        registerShortcut(
          "/",
          (e) => {
            if (suppressed()) return;
            e.preventDefault();
            setOpen(true);
            announcePaletteOpen();
          },
          { preventDefault: true, description: "Search" },
        ),
      );
    }

    return () => unregs.forEach((u) => u());
    // isSuppressed is read through the closure each fire; only re-register if the
    // slash binding flips.
  }, [bindSlash]); // eslint-disable-line react-hooks/exhaustive-deps

  return { open, show, hide, toggle };
}
