import { test } from "vitest";
import assert from "node:assert/strict";
import {
  pluralCategory,
  ordinalCategory,
  selectPluralArm,
  _clearPluralCache,
} from "./plural.ts";

test("English cardinal: 1 → one, else other", () => {
  assert.equal(pluralCategory(1, "en"), "one");
  assert.equal(pluralCategory(0, "en"), "other");
  assert.equal(pluralCategory(2, "en"), "other");
  assert.equal(pluralCategory(100, "en"), "other");
});

test("Polish cardinal has the few/many split", () => {
  // pl: 1 → one; 2-4 (not 12-14) → few; most others → many
  assert.equal(pluralCategory(1, "pl"), "one");
  assert.equal(pluralCategory(2, "pl"), "few");
  assert.equal(pluralCategory(3, "pl"), "few");
  assert.equal(pluralCategory(5, "pl"), "many");
});

test("Arabic cardinal exercises zero/two/few/many", () => {
  assert.equal(pluralCategory(0, "ar"), "zero");
  assert.equal(pluralCategory(1, "ar"), "one");
  assert.equal(pluralCategory(2, "ar"), "two");
  assert.equal(pluralCategory(3, "ar"), "few");
  assert.equal(pluralCategory(11, "ar"), "many");
});

test("Japanese has a single 'other' category", () => {
  assert.equal(pluralCategory(0, "ja"), "other");
  assert.equal(pluralCategory(1, "ja"), "other");
  assert.equal(pluralCategory(5, "ja"), "other");
});

test("English ordinals: 1st→one, 2nd→two, 3rd→few, 4th→other, 11th→other", () => {
  assert.equal(ordinalCategory(1, "en"), "one");
  assert.equal(ordinalCategory(2, "en"), "two");
  assert.equal(ordinalCategory(3, "en"), "few");
  assert.equal(ordinalCategory(4, "en"), "other");
  assert.equal(ordinalCategory(11, "en"), "other");
  assert.equal(ordinalCategory(21, "en"), "one");
});

test("non-finite numbers resolve to 'other'", () => {
  assert.equal(pluralCategory(NaN, "en"), "other");
  assert.equal(pluralCategory(Infinity, "en"), "other");
});

test("unknown locale degrades to the English-ish fallback", () => {
  _clearPluralCache();
  assert.equal(pluralCategory(1, "qqq-XX"), "one");
  assert.equal(pluralCategory(2, "qqq-XX"), "other");
});

test("selectPluralArm: exact =n beats category", () => {
  assert.equal(selectPluralArm(0, "en", ["=0", "one", "other"]), "=0");
  assert.equal(selectPluralArm(1, "en", ["=0", "one", "other"]), "one");
  assert.equal(selectPluralArm(5, "en", ["=0", "one", "other"]), "other");
});

test("selectPluralArm: missing category arm falls to other", () => {
  // Polish 2 → few, but the message only has one/other.
  assert.equal(selectPluralArm(2, "pl", ["one", "other"]), "other");
});

test("selectPluralArm: ordinal type", () => {
  assert.equal(selectPluralArm(2, "en", ["one", "two", "few", "other"], "ordinal"), "two");
});
