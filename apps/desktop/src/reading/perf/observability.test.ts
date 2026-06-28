// Pure §12.5 observability fusion + health grade — node:test. (Type-only sibling
// imports are erased by strip-types, so this self-resolves.)
import test from "node:test";
import assert from "node:assert/strict";
import { buildObservability, gradeHealth, type ObservabilityInput } from "./observability.ts";
import type { FrameStatsSnapshot } from "./frameStats.ts";

const frame = (over: Partial<FrameStatsSnapshot> = {}): FrameStatsSnapshot => ({
  count: 120,
  fps: 60,
  meanMs: 16,
  p95Ms: 18,
  maxMs: 20,
  overBudgetRatio: 0,
  jankCount: 0,
  jankRatio: 0,
  droppedFrames: 0,
  lifetime: { frames: 120, jank: 0, dropped: 0 },
  ...over,
});

test("fuses cores into a flat panel snapshot with rounded headline numbers", () => {
  const input: ObservabilityInput = {
    frame: frame({ fps: 59.97, p95Ms: 18.44, jankRatio: 0.0123, droppedFrames: 2 }),
    decodeHealth: "good",
    decodeDropRate: 0.0156,
    quality: { levelId: "hd", levelLabel: "HD film", tier: "video-hd", reason: "upgrade" },
    buffer: { committedAheadS: 12, bursting: true, zone: "committed" },
    kbps: 8123.7,
    gpuActive: true,
  };
  const s = buildObservability(input);
  assert.equal(s.fps, 60);
  assert.equal(s.p95Ms, 18.4);
  assert.equal(s.jankPct, 1.2);
  assert.equal(s.droppedFrames, 2);
  assert.equal(s.rung, "HD film");
  assert.equal(s.rungReason, "upgrade");
  assert.equal(s.kbps, 8124);
  assert.equal(s.committedAheadS, 12);
  assert.equal(s.bursting, true);
  assert.equal(s.zone, "committed");
  assert.equal(s.gpuActive, true);
  assert.equal(s.grade, "smooth");
});

test("grade: a stalled decoder is struggling regardless of fps", () => {
  assert.equal(gradeHealth({ frame: frame(), decodeHealth: "stalled", decodeDropRate: 0.3 }), "struggling");
});

test("grade: heavy rAF jank is struggling", () => {
  assert.equal(gradeHealth({ frame: frame({ jankRatio: 0.25 }), decodeHealth: "good", decodeDropRate: 0 }), "struggling");
});

test("grade: a degraded decoder or mild jank/low-fps is minor-hitches", () => {
  assert.equal(gradeHealth({ frame: frame(), decodeHealth: "degraded", decodeDropRate: 0.08 }), "minor-hitches");
  assert.equal(gradeHealth({ frame: frame({ fps: 45 }), decodeHealth: "good", decodeDropRate: 0 }), "minor-hitches");
});

test("grade: clean frame + healthy decode is smooth", () => {
  assert.equal(gradeHealth({ frame: frame(), decodeHealth: "good", decodeDropRate: 0 }), "smooth");
});

test("an empty frame snapshot doesn't false-flag as struggling", () => {
  // count 0 → no frame data yet; only decode can drive the grade.
  assert.equal(gradeHealth({ frame: frame({ count: 0, fps: 0 }), decodeHealth: "good", decodeDropRate: 0 }), "smooth");
});
