import { describe, expect, it } from "vitest";

import {
  type BufferPoint,
  type EvalReport,
  METRICS,
  barFraction,
  bufferHealth,
  crewWins,
  improvementPct,
  isEvalReport,
  meetsThreshold,
  metricDomainMax,
  metricThreshold,
  reportToMarkdown,
  reportVerdict,
  summarizeReport,
} from "./report";

/** A report where the crew wins every metric and clears every gate. */
function makeReport(overrides: Partial<EvalReport> = {}): EvalReport {
  return {
    ccs: { crew: 0.93, baseline: 0.71 },
    efficiency: { crew: 96.2, baseline: 78.4 },
    regen_rate: { crew: 0.12, baseline: 0.38 },
    style_drift: { crew: 0.018, baseline: 0.055 },
    runs: 3,
    thresholds: {
      ccs_min: 0.85,
      style_drift_max: 0.05,
      motion_artifact_max: 0.3,
      regen_rate_target: 0.2,
      buffer_above_low_target: 0.99,
      stalls_target: 0,
    },
    per_character_ccs: {
      crew: { arwen: 0.95, gollum: 0.82 },
      baseline: { arwen: 0.74, gollum: 0.6 },
    },
    spread: {
      ccs: { crew: 0.01, baseline: 0.03 },
      efficiency: { crew: 1.2, baseline: 4.1 },
      regen_rate: { crew: 0.02, baseline: 0.05 },
      style_drift: { crew: 0.002, baseline: 0.006 },
    },
    ...overrides,
  };
}

const metaFor = (key: string) => {
  const m = METRICS.find((meta) => meta.key === key);
  if (!m) throw new Error(`no meta for ${key}`);
  return m;
};

describe("crewWins / improvementPct", () => {
  it("treats higher-is-better and lower-is-better correctly", () => {
    expect(crewWins(metaFor("ccs"), { crew: 0.9, baseline: 0.7 })).toBe(true);
    expect(crewWins(metaFor("ccs"), { crew: 0.6, baseline: 0.7 })).toBe(false);
    expect(crewWins(metaFor("regen_rate"), { crew: 0.1, baseline: 0.4 })).toBe(true);
    expect(crewWins(metaFor("regen_rate"), { crew: 0.5, baseline: 0.4 })).toBe(false);
  });

  it("counts a tie as a crew win (no regression)", () => {
    expect(crewWins(metaFor("ccs"), { crew: 0.8, baseline: 0.8 })).toBe(true);
    expect(crewWins(metaFor("style_drift"), { crew: 0.03, baseline: 0.03 })).toBe(true);
  });

  it("signs improvement so positive always means the crew is better", () => {
    expect(improvementPct(metaFor("ccs"), { crew: 1, baseline: 0.5 })).toBeCloseTo(100);
    // lower-is-better: halving the value is a +50% improvement
    expect(improvementPct(metaFor("regen_rate"), { crew: 0.2, baseline: 0.4 })).toBeCloseTo(50);
    expect(improvementPct(metaFor("ccs"), { crew: 0.4, baseline: 0.5 })).toBeCloseTo(-20);
  });

  it("returns null when the baseline is 0 (no ratio)", () => {
    expect(improvementPct(metaFor("style_drift"), { crew: 0.01, baseline: 0 })).toBeNull();
  });
});

describe("bar domains + thresholds", () => {
  it("fixes the CCS and efficiency domains and frames variance metrics by peak", () => {
    expect(metricDomainMax(metaFor("ccs"), { crew: 0.9, baseline: 0.7 })).toBe(1);
    expect(metricDomainMax(metaFor("efficiency"), { crew: 90, baseline: 70 })).toBe(100);
    expect(metricDomainMax(metaFor("regen_rate"), { crew: 0.1, baseline: 0.4 })).toBeCloseTo(0.5);
  });

  it("clamps bar fractions to [0,1]", () => {
    expect(barFraction(0.5, 1)).toBeCloseTo(0.5);
    expect(barFraction(2, 1)).toBe(1);
    expect(barFraction(-1, 1)).toBe(0);
    expect(barFraction(1, 0)).toBe(0);
  });

  it("applies the right pre-registered gate per metric", () => {
    const t = makeReport().thresholds;
    expect(metricThreshold(metaFor("ccs"), t)).toBe(0.85);
    expect(metricThreshold(metaFor("regen_rate"), t)).toBe(0.2);
    expect(metricThreshold(metaFor("style_drift"), t)).toBe(0.05);
    expect(metricThreshold(metaFor("efficiency"), t)).toBeNull();
    expect(meetsThreshold(metaFor("ccs"), 0.9, t)).toBe(true);
    expect(meetsThreshold(metaFor("ccs"), 0.8, t)).toBe(false);
    expect(meetsThreshold(metaFor("style_drift"), 0.04, t)).toBe(true);
    expect(meetsThreshold(metaFor("efficiency"), 95, t)).toBeNull();
  });
});

describe("bufferHealth — mirrors the backend time-weighted definition", () => {
  it("reports a perfect run as fully above L with no stalls", () => {
    const trace: BufferPoint[] = [
      { t: 0, committed_seconds_ahead: 45, low: 25, high: 75 },
      { t: 10, committed_seconds_ahead: 60, low: 25, high: 75 },
      { t: 20, committed_seconds_ahead: 30, low: 25, high: 75 },
    ];
    const h = bufferHealth(trace);
    expect(h.fractionAboveLow).toBeCloseTo(1);
    expect(h.stalls).toBe(0);
    expect(h.durationS).toBeCloseTo(20);
  });

  it("time-weights the fraction below L and counts stall onsets", () => {
    const trace: BufferPoint[] = [
      { t: 0, committed_seconds_ahead: 50, low: 25, high: 75 }, // above (dt 10)
      { t: 10, committed_seconds_ahead: 10, low: 25, high: 75 }, // below (dt 10)
      { t: 20, committed_seconds_ahead: 60, low: 25, high: 75 }, // above (dt 10)
      { t: 30, committed_seconds_ahead: 0, low: 25, high: 75 }, // stall (dt 0)
    ];
    const h = bufferHealth(trace);
    expect(h.fractionAboveLow).toBeCloseTo(20 / 30); // 0.667
    expect(h.stalls).toBe(1);
    expect(h.durationS).toBeCloseTo(30);
  });

  it("handles empty and single-sample traces", () => {
    expect(bufferHealth([])).toEqual({ fractionAboveLow: 1, stalls: 0, durationS: 0 });
    const one = bufferHealth([{ t: 5, committed_seconds_ahead: 40, low: 25, high: 75 }]);
    expect(one.fractionAboveLow).toBe(1);
    expect(one.stalls).toBe(0);
  });
});

describe("verdict + exports", () => {
  it("summarizes the sweep in the headline verdict", () => {
    const v = reportVerdict(makeReport());
    expect(v.wins).toBe(4);
    expect(v.total).toBe(4);
    expect(v.sweep).toBe(true);
    expect(v.ccsGateMet).toBe(true);
    expect(v.gatesTotal).toBe(3);
    expect(v.gatesMet).toBe(3);
    expect(v.headline).toContain("4/4");
  });

  it("flags a missed CCS gate", () => {
    const v = reportVerdict(makeReport({ ccs: { crew: 0.8, baseline: 0.7 } }));
    expect(v.ccsGateMet).toBe(false);
    expect(v.gatesMet).toBe(2);
  });

  it("renders a plain-text summary and a markdown table", () => {
    const report = makeReport();
    const health = bufferHealth([
      { t: 0, committed_seconds_ahead: 45, low: 25, high: 75 },
      { t: 10, committed_seconds_ahead: 60, low: 25, high: 75 },
    ]);
    const text = summarizeReport(report, health);
    expect(text).toContain("Character consistency");
    expect(text).toContain("crew 0.930");
    expect(text).toContain("above L");

    const md = reportToMarkdown(report, health);
    expect(md).toContain("| Metric | Crew | Baseline |");
    expect(md).toContain("96.2%");
    expect(md).toContain("✓");
  });
});

describe("isEvalReport guard", () => {
  it("accepts a real report and rejects junk", () => {
    expect(isEvalReport(makeReport())).toBe(true);
    expect(isEvalReport(null)).toBe(false);
    expect(isEvalReport({ error: { type: "x", message: "y" } })).toBe(false);
    expect(isEvalReport({ ccs: {}, efficiency: {} })).toBe(false);
  });
});
