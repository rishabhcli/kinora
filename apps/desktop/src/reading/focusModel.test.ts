import { describe, it, expect } from "vitest";
import { activeParagraphIndex, focusContentY, focusOpacity } from "./focusModel";

describe("focusContentY", () => {
  it("places the focus line 40% down by default", () => {
    expect(focusContentY(100, 1000)).toBe(500); // 100 + 1000*0.4
  });
  it("honours a custom ratio", () => {
    expect(focusContentY(0, 1000, 0.5)).toBe(500);
  });
});

describe("activeParagraphIndex", () => {
  it("returns the last paragraph whose top crossed the focus line", () => {
    expect(activeParagraphIndex([0, 100, 200, 300], 250)).toBe(2);
  });
  it("returns 0 before any paragraph crosses", () => {
    expect(activeParagraphIndex([100, 200, 300], 50)).toBe(0);
  });
  it("returns the last index when the line is past everything", () => {
    expect(activeParagraphIndex([0, 100, 200], 9999)).toBe(2);
  });
  it("is safe for an empty list", () => {
    expect(activeParagraphIndex([], 100)).toBe(0);
  });
});

describe("focusOpacity", () => {
  it("keeps the active paragraph fully opaque", () => {
    expect(focusOpacity(0)).toBe(1);
  });
  it("never dims below the floor (default 0.78 — gentler than the old 0.62)", () => {
    expect(focusOpacity(5)).toBeCloseTo(0.78, 5);
    expect(focusOpacity(2)).toBeCloseTo(0.78, 5);
  });
  it("ramps softly for near neighbours", () => {
    const one = focusOpacity(1); // 1 - 0.5*(0.22) = 0.89
    expect(one).toBeGreaterThan(0.78);
    expect(one).toBeLessThan(1);
  });
  it("is symmetric (direction-agnostic)", () => {
    expect(focusOpacity(-1)).toBe(focusOpacity(1));
  });
});
