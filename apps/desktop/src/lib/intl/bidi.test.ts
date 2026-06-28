import { test } from "vitest";
import assert from "node:assert/strict";
import {
  isRtl,
  directionOf,
  isolate,
  isolateDir,
  stripBidiControls,
  firstStrongDirection,
  physicalEdge,
  FSI,
  PDI,
} from "./bidi.ts";

test("isRtl recognises RTL languages and their regional variants", () => {
  assert.equal(isRtl("ar"), true);
  assert.equal(isRtl("ar-EG"), true);
  assert.equal(isRtl("he"), true);
  assert.equal(isRtl("fa-IR"), true);
  assert.equal(isRtl("ur"), true);
});

test("isRtl is false for LTR languages", () => {
  assert.equal(isRtl("en"), false);
  assert.equal(isRtl("ja"), false);
  assert.equal(isRtl("zh-Hant"), false);
});

test("isRtl detects an explicit RTL script subtag", () => {
  assert.equal(isRtl("az-Arab"), true);
  assert.equal(isRtl("ku-Arab-IQ"), true);
  assert.equal(isRtl("az-Latn"), false);
});

test("directionOf", () => {
  assert.equal(directionOf("ar"), "rtl");
  assert.equal(directionOf("en"), "ltr");
});

test("isolate wraps in FSI…PDI", () => {
  assert.equal(isolate("123"), `${FSI}123${PDI}`);
  assert.equal(isolate(""), "");
});

test("isolateDir forces a base direction", () => {
  const out = isolateDir("hello", "rtl");
  assert.ok(out.startsWith("⁧"));
  assert.ok(out.endsWith(PDI));
});

test("stripBidiControls removes isolates and marks", () => {
  assert.equal(stripBidiControls(isolate("abc")), "abc");
  assert.equal(stripBidiControls("a‎b‏c"), "abc");
});

test("firstStrongDirection", () => {
  assert.equal(firstStrongDirection("hello"), "ltr");
  assert.equal(firstStrongDirection("שלום"), "rtl");
  assert.equal(firstStrongDirection("مرحبا"), "rtl");
  assert.equal(firstStrongDirection("123 only digits"), "ltr");
  assert.equal(firstStrongDirection("  العربية"), "rtl");
});

test("physicalEdge mirrors only under RTL", () => {
  assert.equal(physicalEdge("left", "ltr"), "left");
  assert.equal(physicalEdge("left", "rtl"), "right");
  assert.equal(physicalEdge("right", "rtl"), "left");
  assert.equal(physicalEdge("start", "rtl"), "start");
});
