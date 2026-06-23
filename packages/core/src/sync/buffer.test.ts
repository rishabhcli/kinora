import { describe, expect, it } from "vitest";

import type { BufferPoint } from "../eval/report";
import {
  advanceSawtoothCursor,
  bufferFraction,
  classifyBufferSurface,
  deriveZone,
  isReaderActive,
  sampleSawtoothAt,
} from "./buffer";

const TRACE: BufferPoint[] = [
  { t: 0, committed_seconds_ahead: 75, low: 25, high: 75 },
  { t: 10, committed_seconds_ahead: 50, low: 25, high: 75 },
  { t: 20, committed_seconds_ahead: 25, low: 25, high: 75 },
  { t: 30, committed_seconds_ahead: 75, low: 25, high: 75 },
];

describe("bufferFraction", () => {
  it("clamps occupancy to [0, 1] of the high watermark", () => {
    expect(bufferFraction(75, 75)).toBe(1);
    expect(bufferFraction(0, 75)).toBe(0);
    expect(bufferFraction(37.5, 75)).toBeCloseTo(0.5);
    expect(bufferFraction(100, 75)).toBe(1); // clamped
    expect(bufferFraction(10, 0)).toBe(0); // guards divide-by-zero
  });
});

describe("sampleSawtoothAt", () => {
  it("returns endpoints outside the range and interpolates within", () => {
    expect(sampleSawtoothAt(TRACE, -5)).toBe(75);
    expect(sampleSawtoothAt(TRACE, 100)).toBe(75);
    expect(sampleSawtoothAt(TRACE, 0)).toBe(75);
    expect(sampleSawtoothAt(TRACE, 5)).toBeCloseTo(62.5); // halfway 75→50
    expect(sampleSawtoothAt(TRACE, 15)).toBeCloseTo(37.5); // halfway 50→25
  });
  it("returns 0 for an empty trace", () => {
    expect(sampleSawtoothAt([], 5)).toBe(0);
  });
});

describe("advanceSawtoothCursor", () => {
  it("advances by dt*rate and loops at tMax", () => {
    expect(advanceSawtoothCursor(0, 1, 30, 6)).toBe(6);
    expect(advanceSawtoothCursor(28, 1, 30, 6)).toBeCloseTo(4); // 34 → wraps to 4
    expect(advanceSawtoothCursor(10, 0, 0, 6)).toBe(0); // no trace
  });
});

describe("isReaderActive", () => {
  it("is true within the window of the last move", () => {
    expect(isReaderActive(1000, 1500, 2200)).toBe(true);
    expect(isReaderActive(1000, 4000, 2200)).toBe(false);
  });
});

describe("deriveZone", () => {
  it("prefers the authoritative zone when present", () => {
    expect(deriveZone({ authoritativeZone: "cold", stage: "full_video", budgetLow: false, fraction: 1 })).toBe("cold");
  });
  it("budget pressure rides the keyframe ladder (preview still)", () => {
    expect(deriveZone({ stage: "full_video", budgetLow: true, fraction: 1 })).toBe("speculative");
  });
  it("full video on the stage with no override is full film", () => {
    expect(deriveZone({ stage: "full_video", budgetLow: false, fraction: 0.9 })).toBe("committed");
  });
  it("an empty buffer on a non-video rung is planning ahead", () => {
    expect(deriveZone({ stage: "keyframe_ken_burns", budgetLow: false, fraction: 0.02 })).toBe("cold");
  });
  it("a keyframe rung with a full-ish buffer is a preview still", () => {
    expect(deriveZone({ stage: "keyframe_ken_burns", budgetLow: false, fraction: 0.6 })).toBe("speculative");
  });
});

describe("classifyBufferSurface — stall (§4.11)", () => {
  it("flags 'Catching up' when the live buffer drains with renders in flight", () => {
    const s = classifyBufferSurface({
      stage: "keyframe_ken_burns",
      budgetLow: false,
      fraction: 0.5,
      active: true,
      liveCommittedAheadS: 0.5,
      inflightCommitted: 2,
    });
    expect(s.stalled).toBe(true);
    expect(s.label).toBe("Catching up");
  });
  it("does NOT flag a stall with the live gate off (no committed renders)", () => {
    const s = classifyBufferSurface({
      stage: "keyframe_ken_burns",
      budgetLow: false,
      fraction: 0.5,
      active: true,
      liveCommittedAheadS: 0,
      inflightCommitted: 0, // nothing rendering → keyframe-by-design, not a stall
    });
    expect(s.stalled).toBe(false);
    expect(s.label).toBe("Preview still");
  });
  it("does NOT flag a stall while watching full video", () => {
    const s = classifyBufferSurface({
      stage: "full_video",
      budgetLow: false,
      fraction: 0.1,
      active: true,
      liveCommittedAheadS: 0.2,
      inflightCommitted: 3,
    });
    expect(s.stalled).toBe(false);
    expect(s.label).toBe("Full film");
  });
});
