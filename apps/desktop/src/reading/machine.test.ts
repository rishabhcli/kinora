// Tests for the open-state machine — the heart of WS1: "fully functional, every
// time". Pure reducer, no React/DOM, runnable with `node --test`.
import test from "node:test";
import assert from "node:assert/strict";
import {
  initialState,
  reduce,
  canReveal,
  filmReady,
  type MachineState,
  type MachineEvent,
} from "./machine.ts";

/** Apply a sequence of events from `initialState`. */
function run(...events: MachineEvent[]): MachineState {
  return events.reduce(reduce, initialState);
}

test("initialState is idle with everything unloaded", () => {
  assert.equal(initialState.phase, "idle");
  assert.equal(initialState.mode, "unknown");
  assert.equal(initialState.animReady, false);
  assert.equal(initialState.error, null);
  assert.deepEqual(initialState.load, {
    meta: false,
    pages: false,
    shots: false,
    session: false,
    firstFrame: false,
  });
});

test("OPEN moves idle -> opening with fresh flags", () => {
  const s = run({ type: "OPEN" });
  assert.equal(s.phase, "opening");
  assert.equal(s.mode, "unknown");
  assert.equal(s.load.meta, false);
});

test("META moves opening -> loading and records meta", () => {
  const s = run({ type: "OPEN" }, { type: "META" });
  assert.equal(s.phase, "loading");
  assert.equal(s.load.meta, true);
});

test("PAGES/SHOTS record flags without leaving loading on their own", () => {
  const s = run({ type: "OPEN" }, { type: "META" }, { type: "PAGES" }, { type: "SHOTS" });
  assert.equal(s.phase, "loading");
  assert.equal(s.load.pages, true);
  assert.equal(s.load.shots, true);
});

test("SESSION enters warming and marks the film live", () => {
  const s = run({ type: "OPEN" }, { type: "META" }, { type: "SHOTS" }, { type: "SESSION" });
  assert.equal(s.phase, "warming");
  assert.equal(s.mode, "live");
  assert.equal(s.load.session, true);
});

test("FALLBACK enters warming as the fallback film and notes the reason", () => {
  const s = run({ type: "OPEN" }, { type: "FALLBACK", message: "no backend" });
  assert.equal(s.phase, "warming");
  assert.equal(s.mode, "fallback");
  assert.equal(s.error, "no backend");
});

test("FIRST_FRAME enters ready", () => {
  const s = run({ type: "OPEN" }, { type: "FALLBACK" }, { type: "FIRST_FRAME" });
  assert.equal(s.phase, "ready");
  assert.equal(s.load.firstFrame, true);
  assert.equal(filmReady(s), true);
});

test("canReveal requires BOTH the first frame and the open animation", () => {
  const frameOnly = run({ type: "OPEN" }, { type: "FALLBACK" }, { type: "FIRST_FRAME" });
  assert.equal(canReveal(frameOnly), false); // anim not ready yet

  const animOnly = run({ type: "OPEN" }, { type: "FALLBACK" }, { type: "ANIM_READY" });
  assert.equal(canReveal(animOnly), false); // film not ready yet

  const both = run(
    { type: "OPEN" },
    { type: "FALLBACK" },
    { type: "ANIM_READY" },
    { type: "FIRST_FRAME" },
  );
  assert.equal(canReveal(both), true);
});

test("canReveal works regardless of whether anim or frame arrives first", () => {
  const frameThenAnim = run(
    { type: "OPEN" },
    { type: "FALLBACK" },
    { type: "FIRST_FRAME" },
    { type: "ANIM_READY" },
  );
  assert.equal(canReveal(frameThenAnim), true);
});

test("REVEAL moves ready -> reading", () => {
  const s = run(
    { type: "OPEN" },
    { type: "FALLBACK" },
    { type: "ANIM_READY" },
    { type: "FIRST_FRAME" },
    { type: "REVEAL" },
  );
  assert.equal(s.phase, "reading");
});

test("REVEAL is ignored before the film is ready", () => {
  const s = run({ type: "OPEN" }, { type: "FALLBACK" }, { type: "REVEAL" });
  assert.equal(s.phase, "warming"); // never jumped to reading prematurely
});

test("CLOSE moves any active phase -> closing; CLOSED resets to idle", () => {
  const closing = run({ type: "OPEN" }, { type: "META" }, { type: "CLOSE" });
  assert.equal(closing.phase, "closing");
  const idle = reduce(closing, { type: "CLOSED" });
  assert.deepEqual(idle, initialState);
});

test("OPEN is a hard reset even mid-reading (rapid re-open)", () => {
  const reading = run(
    { type: "OPEN" },
    { type: "SESSION" },
    { type: "ANIM_READY" },
    { type: "FIRST_FRAME" },
    { type: "REVEAL" },
  );
  assert.equal(reading.phase, "reading");
  const reopened = reduce(reading, { type: "OPEN" });
  assert.equal(reopened.phase, "opening");
  assert.equal(reopened.load.firstFrame, false);
  assert.equal(reopened.mode, "unknown");
});

test("load events are ignored once idle or closing (no resurrection)", () => {
  const afterClose = run({ type: "OPEN" }, { type: "CLOSE" }, { type: "FIRST_FRAME" });
  assert.equal(afterClose.phase, "closing");
  assert.equal(afterClose.load.firstFrame, false);

  const fromIdle = reduce(initialState, { type: "META" });
  assert.deepEqual(fromIdle, initialState);
});

test("phase only moves forward — a late META never drags ready back to loading", () => {
  const ready = run(
    { type: "OPEN" },
    { type: "SESSION" },
    { type: "FIRST_FRAME" },
  );
  assert.equal(ready.phase, "ready");
  const stray = reduce(ready, { type: "META" });
  assert.equal(stray.phase, "ready"); // forward-only
  assert.equal(stray.load.meta, true); // flag still recorded
});

test("an early FIRST_FRAME while still loading records the frame but does NOT reveal", () => {
  // The engine paints the bundled film eagerly, but we keep the warm-up up until
  // we've actually committed to a film source (session or fallback).
  const s = run({ type: "OPEN" }, { type: "META" }, { type: "FIRST_FRAME" });
  assert.equal(s.phase, "loading"); // warm-up stays — ingest still in progress
  assert.equal(s.load.firstFrame, true);
  assert.equal(canReveal(s), false);
});

test("once a session begins with a frame already painted, it goes straight to ready", () => {
  const s = run({ type: "OPEN" }, { type: "META" }, { type: "FIRST_FRAME" }, { type: "SHOTS" }, { type: "SESSION" });
  assert.equal(s.phase, "ready");
});

test("an early FIRST_FRAME then FALLBACK reveals straight to ready", () => {
  const s = run({ type: "OPEN" }, { type: "FIRST_FRAME" }, { type: "FALLBACK" });
  assert.equal(s.phase, "ready");
  assert.equal(s.mode, "fallback");
});

test("FALLBACK after a live SESSION degrades the film without regressing the phase", () => {
  const degraded = run(
    { type: "OPEN" },
    { type: "SESSION" }, // live, warming
    { type: "FALLBACK", message: "render failed" },
  );
  assert.equal(degraded.mode, "fallback");
  assert.equal(degraded.error, "render failed");
  assert.equal(degraded.phase, "warming"); // not dragged backward
});
