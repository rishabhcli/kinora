import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useRovingGrid } from "./useRovingGrid";

describe("useRovingGrid", () => {
  it("starts at the origin cell with tabIndex 0", () => {
    const { result } = renderHook(() => useRovingGrid([3, 5], "g"));
    expect(result.current.active).toEqual({ row: 0, col: 0 });
    expect(result.current.tabIndexFor(0, 0)).toBe(0);
    expect(result.current.tabIndexFor(0, 1)).toBe(-1);
    expect(result.current.idFor(1, 2)).toBe("g-r1-c2");
  });

  it("moves right and down via onKeyDown", () => {
    const { result } = renderHook(() => useRovingGrid([3, 5], "g"));
    act(() => {
      result.current.onKeyDown({ key: "ArrowRight", preventDefault() {} } as never);
    });
    expect(result.current.active).toEqual({ row: 0, col: 1 });
    act(() => {
      result.current.onKeyDown({ key: "ArrowDown", preventDefault() {} } as never);
    });
    expect(result.current.active).toEqual({ row: 1, col: 1 });
  });

  it("clamps the column when moving into a shorter row", () => {
    const { result } = renderHook(() => useRovingGrid([5, 2], "g"));
    act(() => result.current.setActive({ row: 0, col: 4 }));
    act(() => {
      result.current.onKeyDown({ key: "ArrowDown", preventDefault() {} } as never);
    });
    expect(result.current.active).toEqual({ row: 1, col: 1 });
  });

  it("ignores non-navigation keys", () => {
    const { result } = renderHook(() => useRovingGrid([3], "g"));
    act(() => {
      result.current.onKeyDown({ key: "a", preventDefault() {} } as never);
    });
    expect(result.current.active).toEqual({ row: 0, col: 0 });
  });
});
