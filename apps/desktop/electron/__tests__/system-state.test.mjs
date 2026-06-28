import test from "node:test";
import assert from "node:assert/strict";
import { sameSystemState, normalizeThermal } from "../../dist-electron/core/system-state.js";

const base = { online: true, onBattery: false, thermalState: "nominal", suspended: false };

test("sameSystemState is true for identical states", () => {
  assert.equal(sameSystemState(base, { ...base }), true);
});

test("sameSystemState detects each field change", () => {
  assert.equal(sameSystemState(base, { ...base, online: false }), false);
  assert.equal(sameSystemState(base, { ...base, onBattery: true }), false);
  assert.equal(sameSystemState(base, { ...base, thermalState: "serious" }), false);
  assert.equal(sameSystemState(base, { ...base, suspended: true }), false);
});

test("normalizeThermal maps known values and falls back to unknown", () => {
  assert.equal(normalizeThermal("nominal"), "nominal");
  assert.equal(normalizeThermal("critical"), "critical");
  assert.equal(normalizeThermal("weird"), "unknown");
  assert.equal(normalizeThermal(undefined), "unknown");
  assert.equal(normalizeThermal(null), "unknown");
});
