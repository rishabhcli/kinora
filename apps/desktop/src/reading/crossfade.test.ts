// Pure layer reducer behind CrossfadeFilm: swap shot clips by cross-fading
// opacity so the film NEVER hard-cuts to black, capped at two <video> elements.
import test from "node:test";
import assert from "node:assert/strict";
import { pushSrc, markReady, promote, type Layer } from "./crossfade.ts";

const L = (key: number, src: string, ready = false): Layer => ({ key, src, ready });

test("pushSrc with an empty src holds the current frame (no change)", () => {
  const cur = [L(0, "/a.mp4", true)];
  assert.deepEqual(pushSrc(cur, "", 1), cur);
});

test("pushSrc onto an empty stage starts a single layer", () => {
  assert.deepEqual(pushSrc([], "/a.mp4", 7), [{ key: 7, src: "/a.mp4", ready: false }]);
});

test("pushSrc with the same src as the base drops the incoming (no duplicate)", () => {
  const cur = [L(0, "/a.mp4", true)];
  assert.deepEqual(pushSrc(cur, "/a.mp4", 1), cur);
});

test("pushSrc with a new src keeps the base underneath for the cross-fade", () => {
  const cur = [L(0, "/a.mp4", true)];
  const next = pushSrc(cur, "/b.mp4", 1);
  assert.equal(next.length, 2);
  assert.equal(next[0].src, "/a.mp4"); // base retained (no black flash)
  assert.deepEqual(next[1], { key: 1, src: "/b.mp4", ready: false });
});

test("pushSrc caps at two layers — a third src replaces the in-flight incoming", () => {
  const cur = [L(0, "/a.mp4", true), L(1, "/b.mp4", false)];
  const next = pushSrc(cur, "/c.mp4", 2);
  assert.equal(next.length, 2);
  assert.equal(next[0].src, "/a.mp4");
  assert.equal(next[1].src, "/c.mp4");
});

test("markReady flips the matching layer's ready flag", () => {
  const cur = [L(0, "/a.mp4", true), L(1, "/b.mp4", false)];
  const next = markReady(cur, 1, false);
  assert.equal(next[1].ready, true);
  assert.equal(next.length, 2);
});

test("markReady under reduced-motion promotes the incoming instantly (no fade)", () => {
  const cur = [L(0, "/a.mp4", true), L(1, "/b.mp4", false)];
  const next = markReady(cur, 1, true);
  assert.deepEqual(next, [{ key: 1, src: "/b.mp4", ready: true }]);
});

test("markReady ignores an unknown key", () => {
  const cur = [L(0, "/a.mp4", true)];
  assert.deepEqual(markReady(cur, 99, false), cur);
});

test("promote collapses to the incoming once its fade completes", () => {
  const cur = [L(0, "/a.mp4", true), L(1, "/b.mp4", true)];
  assert.deepEqual(promote(cur, 1), [{ key: 1, src: "/b.mp4", ready: true }]);
});

test("promote is a no-op for a single layer or a mismatched key", () => {
  const single = [L(0, "/a.mp4", true)];
  assert.deepEqual(promote(single, 0), single);
  const two = [L(0, "/a.mp4", true), L(1, "/b.mp4", true)];
  assert.deepEqual(promote(two, 0), two); // key 0 isn't the incoming
});
