import test from "node:test";
import assert from "node:assert/strict";
import {
  reconcileWindowState,
  clampSize,
  centeredDefault,
  cascadeFrom,
  isValidBounds,
  DEFAULT_CONSTRAINTS,
} from "../../dist-electron/core/window-state.js";

const display = (id, x, y, w, h) => ({ id, bounds: { x, y, width: w, height: h } });
const ONE_1080P = [display(1, 0, 0, 1920, 1080)];

test("null stored state yields a centred default on primary", () => {
  const s = reconcileWindowState(null, ONE_1080P);
  assert.equal(s.bounds.width, DEFAULT_CONSTRAINTS.defaultWidth);
  assert.equal(s.bounds.height, DEFAULT_CONSTRAINTS.defaultHeight);
  // centred: (1920-1280)/2 = 320, (1080-800)/2 = 140
  assert.equal(s.bounds.x, 320);
  assert.equal(s.bounds.y, 140);
});

test("valid on-screen bounds are preserved", () => {
  const stored = { bounds: { x: 100, y: 100, width: 1000, height: 700 }, maximized: false, fullScreen: false };
  const s = reconcileWindowState(stored, ONE_1080P);
  assert.deepEqual(s.bounds, { x: 100, y: 100, width: 1000, height: 700 });
});

test("oversized bounds are clamped to the largest display", () => {
  const stored = { bounds: { x: 0, y: 0, width: 9000, height: 9000 } };
  const s = reconcileWindowState(stored, ONE_1080P);
  assert.ok(s.bounds.width <= 1920);
  assert.ok(s.bounds.height <= 1080);
});

test("fully off-screen bounds (monitor removed) re-centre on primary", () => {
  // Window saved at (5000,5000) but only a 1080p display remains.
  const stored = { bounds: { x: 5000, y: 5000, width: 1000, height: 700 } };
  const s = reconcileWindowState(stored, ONE_1080P);
  // Re-centred → fully inside the primary display.
  assert.ok(s.bounds.x >= 0 && s.bounds.x + s.bounds.width <= 1920);
  assert.ok(s.bounds.y >= 0 && s.bounds.y + s.bounds.height <= 1080);
});

test("a window straddling two displays stays put", () => {
  const two = [display(1, 0, 0, 1920, 1080), display(2, 1920, 0, 1920, 1080)];
  const stored = { bounds: { x: 1800, y: 100, width: 800, height: 600 } };
  const s = reconcileWindowState(stored, two);
  assert.equal(s.bounds.x, 1800);
});

test("maximized / fullScreen flags survive reconciliation", () => {
  const stored = { bounds: { x: 10, y: 10, width: 1000, height: 700 }, maximized: true, fullScreen: false };
  const s = reconcileWindowState(stored, ONE_1080P);
  assert.equal(s.maximized, true);
  assert.equal(s.fullScreen, false);
});

test("clampSize enforces min width/height", () => {
  const clamped = clampSize({ x: 0, y: 0, width: 10, height: 10 }, ONE_1080P, DEFAULT_CONSTRAINTS);
  assert.equal(clamped.width, DEFAULT_CONSTRAINTS.minWidth);
  assert.equal(clamped.height, DEFAULT_CONSTRAINTS.minHeight);
});

test("centeredDefault centres within an area", () => {
  const b = centeredDefault({ x: 0, y: 0, width: 1000, height: 1000 }, { width: 400, height: 200 });
  assert.deepEqual(b, { x: 300, y: 400, width: 400, height: 200 });
});

test("cascadeFrom offsets and wraps when off the edge", () => {
  const area = { x: 0, y: 0, width: 1000, height: 1000 };
  const off = cascadeFrom({ x: 50, y: 50, width: 400, height: 300 }, area, 28);
  assert.deepEqual(off, { x: 78, y: 78, width: 400, height: 300 });
  // Near the edge: wraps back to the inset origin.
  const wrap = cascadeFrom({ x: 700, y: 800, width: 400, height: 300 }, area, 28);
  assert.equal(wrap.x, 28);
  assert.equal(wrap.y, 28);
});

test("isValidBounds rejects NaN / non-positive / missing", () => {
  assert.equal(isValidBounds({ x: 0, y: 0, width: 100, height: 100 }), true);
  assert.equal(isValidBounds({ x: 0, y: 0, width: 0, height: 100 }), false);
  assert.equal(isValidBounds({ x: NaN, y: 0, width: 100, height: 100 }), false);
  assert.equal(isValidBounds(null), false);
  assert.equal(isValidBounds(undefined), false);
});

test("empty display list still yields a sane default (synthetic primary)", () => {
  const s = reconcileWindowState(null, []);
  assert.ok(s.bounds.width >= DEFAULT_CONSTRAINTS.minWidth);
  assert.ok(s.bounds.height >= DEFAULT_CONSTRAINTS.minHeight);
});
