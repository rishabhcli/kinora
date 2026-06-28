// Roving-tabindex grid navigation — pure index math for keyboard movement across
// a set of horizontal rows (the home shelves). The grid is "ragged": each row
// can have a different number of items (rows[i] = count). Arrow keys move within
// and across rows; Home/End jump to row edges; PageUp/Down jump rows.
//
// Position is a flat {row, col}. The React layer keeps the active cell in state,
// renders tabIndex=0 only on the active cell (the rest -1), and calls `move` on
// keydown. This keeps the whole grid a single tab stop with arrow navigation —
// the WAI-ARIA roving-tabindex pattern.

export interface GridPos {
  row: number;
  col: number;
}

export type RovingKey =
  | "ArrowRight"
  | "ArrowLeft"
  | "ArrowUp"
  | "ArrowDown"
  | "Home"
  | "End"
  | "PageUp"
  | "PageDown";

function clampRow(row: number, rows: number[]): number {
  return Math.max(0, Math.min(rows.length - 1, row));
}

function clampCol(col: number, count: number): number {
  if (count <= 0) return 0;
  return Math.max(0, Math.min(count - 1, col));
}

/**
 * Compute the next position for a key press. `rows` is the per-row item count.
 * - Right/Left move within a row, stopping at the edges (no wrap by default).
 * - Up/Down move to the adjacent row, keeping the column clamped to that row's
 *   length (so moving from a long row into a short one lands on its last item).
 * - Home/End jump to the first/last column of the current row.
 * - PageUp/PageDown jump to the first/last row (same clamped column).
 */
export function move(pos: GridPos, key: RovingKey, rows: number[]): GridPos {
  if (rows.length === 0) return { row: 0, col: 0 };
  const row = clampRow(pos.row, rows);
  const count = rows[row];
  const col = clampCol(pos.col, count);

  switch (key) {
    case "ArrowRight":
      return { row, col: Math.min(col + 1, count - 1) };
    case "ArrowLeft":
      return { row, col: Math.max(col - 1, 0) };
    case "ArrowDown": {
      const nextRow = clampRow(row + 1, rows);
      return { row: nextRow, col: clampCol(col, rows[nextRow]) };
    }
    case "ArrowUp": {
      const prevRow = clampRow(row - 1, rows);
      return { row: prevRow, col: clampCol(col, rows[prevRow]) };
    }
    case "Home":
      return { row, col: 0 };
    case "End":
      return { row, col: Math.max(count - 1, 0) };
    case "PageUp":
      return { row: 0, col: clampCol(col, rows[0]) };
    case "PageDown": {
      const last = rows.length - 1;
      return { row: last, col: clampCol(col, rows[last]) };
    }
  }
}

/** A stable string id for a cell, for `tabIndex`/`ref`/`aria-activedescendant`. */
export function cellId(prefix: string, pos: GridPos): string {
  return `${prefix}-r${pos.row}-c${pos.col}`;
}

/** Is `pos` the currently-active cell? (Used to set tabIndex=0 vs -1.) */
export function isActiveCell(active: GridPos, row: number, col: number): boolean {
  return active.row === row && active.col === col;
}

/** Map a raw KeyboardEvent key to a RovingKey, or null if it's not a nav key. */
export function rovingKeyFor(key: string): RovingKey | null {
  const keys: RovingKey[] = [
    "ArrowRight",
    "ArrowLeft",
    "ArrowUp",
    "ArrowDown",
    "Home",
    "End",
    "PageUp",
    "PageDown",
  ];
  return (keys as string[]).includes(key) ? (key as RovingKey) : null;
}
