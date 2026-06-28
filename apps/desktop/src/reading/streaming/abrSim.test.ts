// ABR trace-replay harness — vitest (imports sibling qualityLadder).
import { describe, expect, it } from "vitest";
import { simulateAbr, levelById, type TraceSample } from "./abrSim";

// Build a trace from {ms-interval, sample} pairs starting at t=0.
function trace(samples: Omit<TraceSample, "t">[], stepMs = 2000): TraceSample[] {
  return samples.map((s, i) => ({ ...s, t: i * stepMs }));
}

describe("simulateAbr", () => {
  it("settles at HD on a fat, stable pipe and barely switches", () => {
    const t = trace(Array.from({ length: 20 }, () => ({ kbps: 30000, bufferAheadS: 30 })));
    const r = simulateAbr(t, { startIndex: 1 });
    // Ends on HD, and the richest index visited is HD (0).
    expect(r.steps[r.steps.length - 1].levelId).toBe("hd");
    expect(r.bestIndex).toBe(0);
    // A single climb from SD→HD ≈ 1 switch (the dwell prevents thrash).
    expect(r.switches).toBeLessThanOrEqual(2);
  });

  it("rides bandwidth down without starving (bottom rung is reachable)", () => {
    const t = trace([
      { kbps: 30000, bufferAheadS: 30 },
      { kbps: 5000, bufferAheadS: 20 },
      { kbps: 1500, bufferAheadS: 10 },
      { kbps: 400, bufferAheadS: 6 },
      { kbps: 50, bufferAheadS: 3 },
    ]);
    const r = simulateAbr(t, { startIndex: 0 });
    // We should reach a lean rung at the end and never be stuck above the floor
    // while starving (the controller drops on the way down without dwell).
    expect(["pan", "still", "audio"]).toContain(r.steps[r.steps.length - 1].levelId);
    expect(r.worstIndex).toBeGreaterThan(0);
  });

  it("counts dwell time per rung", () => {
    const t = trace([{ kbps: 200, bufferAheadS: 10 }, { kbps: 200, bufferAheadS: 10 }], 1000);
    const r = simulateAbr(t);
    const totalDwell = Object.values(r.dwellMsByLevel).reduce((a, b) => a + b, 0);
    // Two samples 1000ms apart → 1000ms of dwell attributed (last sample has 0).
    expect(totalDwell).toBe(1000);
  });

  it("a decode stall forces a floor regardless of abundant bandwidth", () => {
    const t = trace([
      { kbps: 50000, bufferAheadS: 30 },
      { kbps: 50000, bufferAheadS: 30, decodeHealth: "stalled" },
    ]);
    const r = simulateAbr(t, { startIndex: 0 });
    const pan = levelById("pan")!;
    expect(r.steps[1].index).toBeGreaterThanOrEqual(DEFAULT_PAN_INDEX(pan.id));
  });

  it("an empty trace yields zeroed metrics", () => {
    const r = simulateAbr([]);
    expect(r.steps).toHaveLength(0);
    expect(r.switches).toBe(0);
    expect(r.meanIndex).toBe(0);
  });
});

function DEFAULT_PAN_INDEX(id: string): number {
  // pan is the 3rd rung (index 2) in the default ladder.
  return id === "pan" ? 2 : 0;
}
