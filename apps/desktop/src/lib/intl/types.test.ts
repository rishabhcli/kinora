import { test } from "vitest";
import assert from "node:assert/strict";
import {
  isMessageTree,
  isSeedLocale,
  normalizeTag,
  primarySubtag,
  truncationChain,
  PLURAL_CATEGORIES,
  SEED_LOCALES,
} from "./types.ts";

test("normalizeTag: language lowercased, region uppercased, script titlecased", () => {
  assert.equal(normalizeTag("EN-us"), "en-US");
  assert.equal(normalizeTag("zh-hant-tw"), "zh-Hant-TW");
  assert.equal(normalizeTag("PT_br"), "pt-BR");
  assert.equal(normalizeTag("FR"), "fr");
});

test("normalizeTag: empty / odd inputs are returned safely", () => {
  assert.equal(normalizeTag(""), "");
  assert.equal(normalizeTag("---"), "");
});

test("primarySubtag returns the language only", () => {
  assert.equal(primarySubtag("en-US"), "en");
  assert.equal(primarySubtag("zh-Hant-TW"), "zh");
  assert.equal(primarySubtag("ja"), "ja");
});

test("truncationChain produces most-specific-first fallbacks", () => {
  assert.deepEqual(truncationChain("zh-Hant-TW"), ["zh-Hant-TW", "zh-Hant", "zh"]);
  assert.deepEqual(truncationChain("pt-BR"), ["pt-BR", "pt"]);
  assert.deepEqual(truncationChain("en"), ["en"]);
});

test("isSeedLocale recognises shipped catalogs", () => {
  assert.equal(isSeedLocale("en"), true);
  assert.equal(isSeedLocale("ar"), true);
  assert.equal(isSeedLocale("pt-BR"), true);
  assert.equal(isSeedLocale("xx"), false);
});

test("isMessageTree distinguishes subtrees from leaves", () => {
  assert.equal(isMessageTree({ a: "b" }), true);
  assert.equal(isMessageTree("leaf"), false);
  assert.equal(isMessageTree(null), false);
  assert.equal(isMessageTree(["a"]), false);
  assert.equal(isMessageTree(new Date()), false);
});

test("PLURAL_CATEGORIES + SEED_LOCALES are the expected sets", () => {
  assert.deepEqual([...PLURAL_CATEGORIES], ["zero", "one", "two", "few", "many", "other"]);
  assert.ok(SEED_LOCALES.includes("en"));
  assert.ok(SEED_LOCALES.includes("ja"));
  assert.equal(SEED_LOCALES.length, 9);
});
