import { useEffect, useState } from "react";

/** Tailwind `lg` — the breakpoint where the reading room uses a side-by-side split. */
const WIDE_QUERY = "(min-width: 1024px)";

/**
 * True when the viewport is wide enough for the desktop reading room's
 * two-pane layout (PDF left, film right). Below this width we fall back to the
 * mobile-style Read / Watch segmented control.
 */
export function useWideLayout(): boolean {
  const [wide, setWide] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia(WIDE_QUERY).matches : true,
  );

  useEffect(() => {
    const mq = window.matchMedia(WIDE_QUERY);
    const onChange = (event: MediaQueryListEvent) => setWide(event.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return wide;
}
