// Pure colour-grade math (CPU reference mirroring the GLSL) — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { NEUTRAL_GRADE, GRADE_PRESETS, applyGrade, gradeByName, lerpGrade } from "./grade.ts";

const close = (a: number, b: number, t = 1e-6) => assert.ok(Math.abs(a - b) <= t, `${a} vs ${b}`);

test("the neutral grade is identity", () => {
  const c = applyGrade([0.2, 0.5, 0.8], NEUTRAL_GRADE);
  close(c[0], 0.2);
  close(c[1], 0.5);
  close(c[2], 0.8);
});

test("zero saturation collapses to luma (greyscale)", () => {
  const grey = applyGrade([0.2, 0.5, 0.8], { ...NEUTRAL_GRADE, saturation: 0 });
  const luma = 0.2126 * 0.2 + 0.7152 * 0.5 + 0.0722 * 0.8;
  close(grey[0], luma);
  close(grey[1], luma);
  close(grey[2], luma);
});

test("gain brightens, output stays clamped to [0,1]", () => {
  const c = applyGrade([0.5, 0.5, 0.5], { ...NEUTRAL_GRADE, gain: [2, 2, 2] });
  // 0.5*2 = 1.0 (clamped), gamma 1 keeps it.
  close(c[0], 1);
  const over = applyGrade([0.9, 0.9, 0.9], { ...NEUTRAL_GRADE, gain: [4, 4, 4] });
  assert.ok(over[0] <= 1 && over[0] >= 0);
});

test("gradeByName resolves presets and falls back to neutral", () => {
  assert.equal(gradeByName("warm"), GRADE_PRESETS.warm);
  assert.equal(gradeByName("nope"), NEUTRAL_GRADE);
  assert.equal(gradeByName(null), NEUTRAL_GRADE);
});

test("lerpGrade at t=0 / t=1 returns the endpoints' values", () => {
  const mid = lerpGrade(NEUTRAL_GRADE, GRADE_PRESETS.warm, 0);
  assert.deepEqual(mid.gain, NEUTRAL_GRADE.gain);
  const end = lerpGrade(NEUTRAL_GRADE, GRADE_PRESETS.warm, 1);
  assert.deepEqual(end.gain, GRADE_PRESETS.warm.gain);
  // Halfway saturation is the average of the two.
  const half = lerpGrade(NEUTRAL_GRADE, GRADE_PRESETS.warm, 0.5);
  close(half.saturation, (NEUTRAL_GRADE.saturation + GRADE_PRESETS.warm.saturation) / 2);
});

test("lerpGrade clamps t", () => {
  const below = lerpGrade(NEUTRAL_GRADE, GRADE_PRESETS.cool, -5);
  assert.deepEqual(below.gain, NEUTRAL_GRADE.gain);
  const above = lerpGrade(NEUTRAL_GRADE, GRADE_PRESETS.cool, 5);
  assert.deepEqual(above.gain, GRADE_PRESETS.cool.gain);
});
