import { describe, expect, it } from "vitest";

import {
  KEN_BURNS_FREEZE_WPS,
  KEN_BURNS_PRESET_COUNT,
  kenBurnsPreset,
  kenBurnsTempo,
} from "./kenburns";

describe("kenBurnsPreset", () => {
  it("is deterministic — the same seed always picks the same move", () => {
    const a = kenBurnsPreset("beat_0042");
    const b = kenBurnsPreset("beat_0042");
    expect(a).toEqual(b);
  });

  it("stays over-scaled throughout so a still never exposes an edge while panning", () => {
    for (let i = 0; i < 200; i++) {
      const p = kenBurnsPreset(`beat_${i}`);
      expect(p.fromScale).toBeGreaterThan(1);
      expect(p.toScale).toBeGreaterThan(1);
      expect(p.durationS).toBeGreaterThan(0);
    }
  });

  it("spreads seeds across the available moves (not all identical)", () => {
    const seen = new Set<number>();
    for (let i = 0; i < 200; i++) seen.add(kenBurnsPreset(`beat_${i}`).durationS);
    // Distinct durations are a proxy for distinct presets being selected.
    expect(seen.size).toBeGreaterThan(1);
    expect(seen.size).toBeLessThanOrEqual(KEN_BURNS_PRESET_COUNT);
  });

  it("falls back to a stable default for an empty seed", () => {
    expect(kenBurnsPreset(null)).toEqual(kenBurnsPreset(""));
  });
});

describe("kenBurnsTempo", () => {
  it("freezes the pan once the reader is skimming (§4.6)", () => {
    expect(kenBurnsTempo(KEN_BURNS_FREEZE_WPS).paused).toBe(true);
    expect(kenBurnsTempo(25).paused).toBe(true);
  });

  it("gives the full lively pan at a dwell and a calmer, slower drift as pace quickens", () => {
    const dwell = kenBurnsTempo(0);
    const brisk = kenBurnsTempo(6);
    expect(dwell.paused).toBe(false);
    expect(dwell.durationScale).toBeCloseTo(1, 5);
    expect(brisk.paused).toBe(false);
    expect(brisk.durationScale).toBeGreaterThan(dwell.durationScale);
  });

  it("treats a non-finite velocity as a dwell (no motion glitch)", () => {
    expect(kenBurnsTempo(Number.NaN)).toEqual({ paused: false, durationScale: 1 });
  });
});
