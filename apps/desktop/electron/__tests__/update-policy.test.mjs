import test from "node:test";
import assert from "node:assert/strict";
import {
  isInRolloutCohort,
  stableBucket,
  shouldAutoCheck,
  reduceUpdateStatus,
  canInstall,
  DEFAULT_ROLLOUT,
} from "../../dist-electron/core/update-policy.js";

test("stableBucket is deterministic and in 0..99", () => {
  const a = stableBucket("machine-A");
  assert.equal(a, stableBucket("machine-A"));
  assert.ok(a >= 0 && a < 100);
  assert.notEqual(stableBucket("machine-A"), stableBucket("machine-B"));
});

test("0% cohort is always out, 100% always in", () => {
  assert.equal(isInRolloutCohort("x", 0), false);
  assert.equal(isInRolloutCohort("x", 100), true);
  assert.equal(isInRolloutCohort("x", 150), true);
  assert.equal(isInRolloutCohort("x", -5), false);
});

test("cohort membership is monotonic as percent grows", () => {
  // A machine in at P% must still be in at any higher percent.
  const id = "monotonic-machine";
  const bucket = stableBucket(id);
  for (let pct = 0; pct <= 100; pct++) {
    assert.equal(isInRolloutCohort(id, pct), bucket < pct);
  }
});

test("a 25% rollout includes roughly a quarter of machines", () => {
  let inCohort = 0;
  const N = 2000;
  for (let i = 0; i < N; i++) if (isInRolloutCohort(`m-${i}`, 25)) inCohort++;
  const ratio = inCohort / N;
  assert.ok(ratio > 0.18 && ratio < 0.32, `ratio=${ratio}`);
});

test("shouldAutoCheck gates on enabled, cohort, and interval", () => {
  const cfg = { ...DEFAULT_ROLLOUT, rolloutPercent: 100, checkIntervalMs: 1000 };
  assert.equal(shouldAutoCheck({ ...cfg, enabled: false }, "m", null, 0), false);
  assert.equal(shouldAutoCheck(cfg, "m", null, 5000), true); // never checked
  assert.equal(shouldAutoCheck(cfg, "m", 4500, 5000), false); // too soon (500 < 1000)
  assert.equal(shouldAutoCheck(cfg, "m", 3000, 5000), true); // 2000 >= 1000
  assert.equal(shouldAutoCheck({ ...cfg, rolloutPercent: 0 }, "m", null, 5000), false);
});

test("reduceUpdateStatus transitions through the lifecycle", () => {
  let s = { phase: "idle" };
  s = reduceUpdateStatus(s, { type: "checking" }, true);
  assert.equal(s.phase, "checking");
  assert.equal(s.stagedRollout, true);
  s = reduceUpdateStatus(s, { type: "available", version: "1.2.3" }, true);
  assert.equal(s.phase, "available");
  assert.equal(s.version, "1.2.3");
  s = reduceUpdateStatus(s, { type: "progress", percent: 42.7, bytesPerSecond: 1000.4 }, true);
  assert.equal(s.phase, "downloading");
  assert.equal(s.percent, 43);
  assert.equal(s.bytesPerSecond, 1000);
  s = reduceUpdateStatus(s, { type: "downloaded", version: "1.2.3" }, true);
  assert.equal(s.phase, "downloaded");
  assert.equal(s.percent, 100);
});

test("reduceUpdateStatus clamps progress percent", () => {
  const s = reduceUpdateStatus({ phase: "available" }, { type: "progress", percent: 250, bytesPerSecond: -5 }, false);
  assert.equal(s.percent, 100);
  assert.equal(s.bytesPerSecond, 0);
});

test("reduce handles error + disabled + not-available", () => {
  assert.equal(reduceUpdateStatus({ phase: "idle" }, { type: "error", message: "x" }, false).phase, "error");
  assert.equal(reduceUpdateStatus({ phase: "idle" }, { type: "disabled" }, false).phase, "disabled");
  assert.equal(reduceUpdateStatus({ phase: "checking" }, { type: "not-available" }, false).phase, "not-available");
});

test("canInstall only for downloaded", () => {
  assert.equal(canInstall({ phase: "downloaded" }), true);
  assert.equal(canInstall({ phase: "available" }), false);
  assert.equal(canInstall({ phase: "idle" }), false);
});
