// Pure asymmetric-EWMA bandwidth estimator — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { BandwidthEstimator } from "./bandwidth.ts";

test("seeds at the initial estimate before any sample", () => {
  const b = new BandwidthEstimator({ initialKbps: 5000 });
  assert.equal(b.kbps(), 5000);
  assert.equal(b.sampleCount, 0);
});

test("a sample of 1Mbit in 1s reads ~1000kbps instant and moves the estimate", () => {
  const b = new BandwidthEstimator({ initialKbps: 5000, downAlpha: 1 });
  // 125000 bytes = 1,000,000 bits over 1000ms = 1000 kbps.
  const est = b.addSample({ bytes: 125000, durationMs: 1000 });
  assert.equal(est, 1000); // downAlpha 1 → jumps straight to the instant value
  assert.equal(b.sampleCount, 1);
});

test("downward moves are faster than upward (conservative)", () => {
  const up = new BandwidthEstimator({ initialKbps: 1000, upAlpha: 0.25, downAlpha: 0.55 });
  const down = new BandwidthEstimator({ initialKbps: 1000, upAlpha: 0.25, downAlpha: 0.55 });
  // 250000 bytes / 1000ms = 2000 kbps (an upward sample from 1000).
  up.addSample({ bytes: 250000, durationMs: 1000 });
  // 62500 bytes / 1000ms = 500 kbps (a downward sample from 1000).
  down.addSample({ bytes: 62500, durationMs: 1000 });
  const upDelta = up.kbps() - 1000; // toward 2000 with alpha 0.25 → +250
  const downDelta = 1000 - down.kbps(); // toward 500 with alpha 0.55 → -275
  assert.ok(Math.abs(upDelta - 250) < 1e-6);
  assert.ok(Math.abs(downDelta - 275) < 1e-6);
  assert.ok(downDelta > upDelta, "downward reacts harder");
});

test("tiny / zero-duration samples are ignored", () => {
  const b = new BandwidthEstimator({ initialKbps: 3000, minBytes: 8192 });
  b.addSample({ bytes: 100, durationMs: 50 }); // below minBytes
  b.addSample({ bytes: 999999, durationMs: 0 }); // bad duration
  b.addSample({ bytes: 999999, durationMs: -10 });
  assert.equal(b.kbps(), 3000);
  assert.equal(b.sampleCount, 0);
});

test("mbps mirrors kbps/1000 and reset re-seeds", () => {
  const b = new BandwidthEstimator({ initialKbps: 8000 });
  assert.equal(b.mbps(), 8);
  b.addSample({ bytes: 125000, durationMs: 1000 });
  b.reset(2000);
  assert.equal(b.kbps(), 2000);
  assert.equal(b.sampleCount, 0);
});
