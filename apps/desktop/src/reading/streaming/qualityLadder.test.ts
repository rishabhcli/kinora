// Pure adaptive-quality ladder + controller — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_LADDER,
  QualityController,
  selectQuality,
} from "./qualityLadder.ts";

const idx = (id: string) => DEFAULT_LADDER.findIndex((l) => l.id === id);

test("abundant bandwidth + safe buffer selects HD", () => {
  const d = selectQuality({ kbps: 20000, bufferAheadS: 20 }, idx("sd"));
  assert.equal(d.level.id, "hd");
  assert.equal(d.reason, "upgrade");
});

test("low bandwidth steps down toward the leaner rungs", () => {
  // 900kbps: with the 1.3× upgrade headroom it clears keyframe-pan (600×1.3=780)
  // but not SD (1800×1.3); from HD that's a downgrade to the pan rung.
  const d = selectQuality({ kbps: 900, bufferAheadS: 20 }, idx("hd"));
  assert.equal(d.level.id, "pan");
  assert.equal(d.reason, "downgrade-bandwidth");
});

test("upgrade headroom: a rung we're not on needs minKbps × headroom to be picked", () => {
  // 700kbps is above pan's raw minKbps (600) but below 600×1.3 — so from HD we
  // skip pan and land on the next rung that clears its headroom'd threshold.
  const d = selectQuality({ kbps: 700, bufferAheadS: 20 }, idx("hd"));
  assert.equal(d.level.id, "still");
});

test("the bottom rung is always reachable (no black pane)", () => {
  const d = selectQuality({ kbps: 0, bufferAheadS: 0 }, idx("hd"));
  assert.equal(d.level.id, "audio");
  assert.equal(d.level.tier, "audio-text");
});

test("a stalled decoder forces at least a keyframe-pan floor regardless of bandwidth", () => {
  const d = selectQuality({ kbps: 50000, bufferAheadS: 30, decodeHealth: "stalled" }, idx("hd"));
  assert.ok(d.index >= idx("pan"), `index ${d.index}`);
  assert.ok(d.reason.startsWith("downgrade-stalled"));
});

test("an unsafe buffer blocks an upgrade even with bandwidth", () => {
  const d = selectQuality({ kbps: 20000, bufferAheadS: 1 }, idx("pan"), { safeBufferS: 4 });
  assert.equal(d.index, idx("pan"));
  assert.equal(d.reason, "hold-unsafe-buffer");
});

test("data-saver caps out of HD", () => {
  const d = selectQuality({ kbps: 50000, bufferAheadS: 30, saveData: true }, idx("sd"));
  assert.notEqual(d.level.tier, "video-hd");
});

test("a device height ceiling caps the rung", () => {
  // maxHeight 720 forbids the 1280-tall HD rung.
  const d = selectQuality({ kbps: 50000, bufferAheadS: 30, maxHeight: 720 }, idx("audio"));
  assert.ok(d.level.height <= 720);
  assert.equal(d.level.id, "sd");
});

test("high rAF jank floors at SD", () => {
  const d = selectQuality({ kbps: 50000, bufferAheadS: 30, jankRatio: 0.4 }, idx("hd"));
  assert.ok(d.index >= idx("sd"));
});

test("controller holds an upgrade until the dwell elapses, but downgrades immediately", () => {
  const c = new QualityController({ upgradeDwellMs: 8000 }, idx("pan"));
  // Plenty of bandwidth wants HD, but we just (implicitly) started — first update
  // sets the dwell anchor and applies the upgrade.
  const first = c.update({ kbps: 20000, bufferAheadS: 20 }, 0);
  assert.equal(first.level.id, "hd");
  // A drop in bandwidth must downgrade right away (no dwell on the way down).
  const drop = c.update({ kbps: 200, bufferAheadS: 20 }, 100);
  assert.equal(drop.level.id, "still");
  // Bandwidth recovers immediately, but the upgrade dwell holds us at "still".
  const held = c.update({ kbps: 20000, bufferAheadS: 20 }, 200);
  assert.equal(held.level.id, "still");
  assert.equal(held.reason, "upgrade-dwell");
  // After the dwell, the upgrade lands.
  const up = c.update({ kbps: 20000, bufferAheadS: 20 }, 9000);
  assert.equal(up.level.id, "hd");
});

test("controller reset returns to the safe default rung", () => {
  const c = new QualityController({}, idx("hd"));
  c.update({ kbps: 100, bufferAheadS: 1 }, 0);
  c.reset(idx("sd"));
  assert.equal(c.current().id, "sd");
});
