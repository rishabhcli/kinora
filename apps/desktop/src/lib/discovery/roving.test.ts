import { describe, it, expect } from "vitest";
import { move, cellId, isActiveCell, rovingKeyFor } from "./roving";

// Ragged grid: row0 has 3, row1 has 5, row2 has 2.
const rows = [3, 5, 2];

describe("move", () => {
  it("moves right/left without wrapping", () => {
    expect(move({ row: 0, col: 0 }, "ArrowRight", rows)).toEqual({ row: 0, col: 1 });
    expect(move({ row: 0, col: 2 }, "ArrowRight", rows)).toEqual({ row: 0, col: 2 }); // edge
    expect(move({ row: 0, col: 0 }, "ArrowLeft", rows)).toEqual({ row: 0, col: 0 }); // edge
  });

  it("moves down/up clamping the column into the target row", () => {
    // from row1 col4 down to row2 (len 2) → clamp to col1
    expect(move({ row: 1, col: 4 }, "ArrowDown", rows)).toEqual({ row: 2, col: 1 });
    // from row0 up stays at row0
    expect(move({ row: 0, col: 1 }, "ArrowUp", rows)).toEqual({ row: 0, col: 1 });
  });

  it("Home/End jump to row edges", () => {
    expect(move({ row: 1, col: 3 }, "Home", rows)).toEqual({ row: 1, col: 0 });
    expect(move({ row: 1, col: 0 }, "End", rows)).toEqual({ row: 1, col: 4 });
  });

  it("PageUp/PageDown jump to first/last row", () => {
    expect(move({ row: 1, col: 1 }, "PageUp", rows)).toEqual({ row: 0, col: 1 });
    expect(move({ row: 0, col: 1 }, "PageDown", rows)).toEqual({ row: 2, col: 1 });
  });

  it("handles an empty grid", () => {
    expect(move({ row: 0, col: 0 }, "ArrowRight", [])).toEqual({ row: 0, col: 0 });
  });

  it("clamps an out-of-range starting position", () => {
    expect(move({ row: 9, col: 9 }, "ArrowLeft", rows)).toEqual({ row: 2, col: 0 });
  });
});

describe("cellId / isActiveCell", () => {
  it("builds a stable id", () => {
    expect(cellId("home", { row: 1, col: 2 })).toBe("home-r1-c2");
  });
  it("detects the active cell", () => {
    expect(isActiveCell({ row: 1, col: 2 }, 1, 2)).toBe(true);
    expect(isActiveCell({ row: 1, col: 2 }, 1, 3)).toBe(false);
  });
});

describe("rovingKeyFor", () => {
  it("recognizes nav keys and rejects others", () => {
    expect(rovingKeyFor("ArrowRight")).toBe("ArrowRight");
    expect(rovingKeyFor("PageDown")).toBe("PageDown");
    expect(rovingKeyFor("a")).toBeNull();
    expect(rovingKeyFor("Enter")).toBeNull();
  });
});
