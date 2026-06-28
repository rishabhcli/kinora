// useRovingGrid — React glue for the roving-tabindex grid (lib/discovery/roving).
// Tracks the active {row,col} cell, maps arrow/Home/End/PageUp/Down to moves over
// a ragged grid (per-row item counts), and moves DOM focus to the newly-active
// cell by id (cellId). The discovery rails register one keydown handler on a
// wrapping element; only the active cell carries tabIndex 0 (the rest -1), so the
// whole grid is a single tab stop with arrow navigation — the WAI-ARIA pattern.
import { useCallback, useRef, useState } from "react";
import { move, cellId, rovingKeyFor, type GridPos } from "../../lib/discovery/roving";

export interface RovingGrid {
  active: GridPos;
  /** tabIndex for a cell (0 when active, -1 otherwise). */
  tabIndexFor: (row: number, col: number) => 0 | -1;
  /** Stable DOM id for a cell, used as ref key + focus target. */
  idFor: (row: number, col: number) => string;
  /** Attach to the grid container; handles arrow nav + focus movement. */
  onKeyDown: (e: React.KeyboardEvent) => void;
  /** Set the active cell (e.g. on focus/click of a card). */
  setActive: (pos: GridPos) => void;
}

export function useRovingGrid(rowSizes: number[], prefix = "grid"): RovingGrid {
  const [active, setActiveState] = useState<GridPos>({ row: 0, col: 0 });
  // rowSizes can change between renders; read the latest in the handler.
  const sizesRef = useRef(rowSizes);
  sizesRef.current = rowSizes;

  const focusCell = useCallback(
    (pos: GridPos) => {
      if (typeof document === "undefined") return;
      const el = document.getElementById(cellId(prefix, pos));
      el?.focus?.();
    },
    [prefix],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const key = rovingKeyFor(e.key);
      if (!key) return;
      e.preventDefault();
      setActiveState((cur) => {
        const next = move(cur, key, sizesRef.current);
        // Defer focus so the new tabIndex is applied before we move focus.
        requestAnimationFrame(() => focusCell(next));
        return next;
      });
    },
    [focusCell],
  );

  const tabIndexFor = useCallback(
    (row: number, col: number): 0 | -1 => (active.row === row && active.col === col ? 0 : -1),
    [active],
  );

  const idFor = useCallback((row: number, col: number) => cellId(prefix, { row, col }), [prefix]);

  const setActive = useCallback((pos: GridPos) => setActiveState(pos), []);

  return { active, tabIndexFor, idFor, onKeyDown, setActive };
}
