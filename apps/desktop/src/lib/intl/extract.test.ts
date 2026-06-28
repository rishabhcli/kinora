import { test } from "vitest";
import assert from "node:assert/strict";
import { extractKeys, extractKeySet, crossReference } from "./extract.ts";

test("extracts t() call keys", () => {
  const src = `const x = t("nav.home");\nconst y = t('common.save');`;
  const { keys } = extractKeys(src);
  assert.deepEqual(
    keys.map((k) => k.key).sort(),
    ["common.save", "nav.home"],
  );
});

test("records the line number", () => {
  const src = `line one\nconst a = t("k.a");\nconst b = t("k.b");`;
  const { keys } = extractKeys(src);
  const a = keys.find((k) => k.key === "k.a");
  assert.equal(a?.line, 2);
});

test("recognises i18n.t and tx forms", () => {
  const src = `i18n.t("a.b"); tx("c.d");`;
  const set = new Set(extractKeys(src).keys.map((k) => k.key));
  assert.ok(set.has("a.b"));
  assert.ok(set.has("c.d"));
});

test("recognises <Trans i18nKey>", () => {
  const src = `<Trans i18nKey="login.tagline" />`;
  const keys = extractKeys(src).keys;
  assert.equal(keys[0].key, "login.tagline");
  assert.equal(keys[0].via, "Trans");
});

test("counts dynamic (non-literal) keys separately", () => {
  const src = `t(dynamicKey); t("literal.one");`;
  const { keys, dynamic } = extractKeys(src);
  assert.equal(dynamic, 1);
  assert.equal(keys.length, 1);
});

test("extractKeySet dedups across sources", () => {
  const set = extractKeySet([`t("a")`, `t("a")`, `t("b")`]);
  assert.deepEqual([...set].sort(), ["a", "b"]);
});

test("crossReference finds undefined + unused keys", () => {
  const used = new Set(["nav.home", "nav.missing"]);
  const catalog = ["nav.home", "nav.library"];
  const report = crossReference(used, catalog);
  assert.deepEqual(report.undefinedKeys, ["nav.missing"]);
  assert.deepEqual(report.unusedKeys, ["nav.library"]);
});
