import { describe, expect, it } from "vitest";

import { bufferFillFraction, bufferHealth, lowMarkFraction } from "./buffer";

describe("bufferFillFraction", () => {
  it("fills linearly toward the high watermark", () => {
    expect(bufferFillFraction(0, 75)).toBe(0);
    expect(bufferFillFraction(37.5, 75)).toBeCloseTo(0.5);
    expect(bufferFillFraction(75, 75)).toBe(1);
  });
  it("clamps above the high watermark", () => {
    expect(bufferFillFraction(120, 75)).toBe(1);
  });
});

describe("lowMarkFraction", () => {
  it("places L as a fraction of H", () => {
    expect(lowMarkFraction(25, 75)).toBeCloseTo(1 / 3);
  });
});

describe("bufferHealth", () => {
  it("is low below L, full at/above H, ok between", () => {
    expect(bufferHealth(10, 25, 75)).toBe("low");
    expect(bufferHealth(25, 25, 75)).toBe("ok");
    expect(bufferHealth(50, 25, 75)).toBe("ok");
    expect(bufferHealth(75, 25, 75)).toBe("full");
    expect(bufferHealth(90, 25, 75)).toBe("full");
  });
});
