import { test } from "vitest";
import assert from "node:assert/strict";
import {
  flatten,
  unflatten,
  deepMerge,
  diffCatalogs,
  getMessage,
  coverage,
  keysOf,
} from "./catalog.ts";

const TREE = {
  nav: { home: "Home", library: "Library" },
  common: { save: "Save" },
};

test("flatten produces dotted keys", () => {
  assert.deepEqual(flatten(TREE), {
    "nav.home": "Home",
    "nav.library": "Library",
    "common.save": "Save",
  });
});

test("unflatten round-trips flatten", () => {
  assert.deepEqual(unflatten(flatten(TREE)), TREE);
});

test("deepMerge layers override onto base without mutating", () => {
  const base = { nav: { home: "Home", about: "About" } };
  const override = { nav: { home: "Inicio" }, extra: "x" };
  const merged = deepMerge(base, override);
  assert.deepEqual(merged, {
    nav: { home: "Inicio", about: "About" },
    extra: "x",
  });
  // base untouched
  assert.equal(base.nav.home, "Home");
});

test("diffCatalogs finds missing/extra/common", () => {
  const ref = { a: "A", b: "B", nested: { c: "C" } };
  const sub = { a: "Ä", nested: { c: "Ç", d: "D" } };
  const diff = diffCatalogs(ref, sub);
  assert.deepEqual(diff.missing, ["b"]);
  assert.deepEqual(diff.extra, ["nested.d"]);
  assert.deepEqual(diff.common, ["a", "nested.c"]);
});

test("getMessage resolves a dotted key", () => {
  assert.equal(getMessage(TREE, "nav.home"), "Home");
  assert.equal(getMessage(TREE, "nav.missing"), undefined);
  assert.equal(getMessage(TREE, "nav"), undefined); // a branch, not a leaf
});

test("coverage ratio", () => {
  const ref = { a: "A", b: "B", c: "C", d: "D" };
  const sub = { a: "A", b: "B" };
  assert.equal(coverage(ref, sub), 0.5);
  assert.equal(coverage(ref, ref), 1);
});

test("keysOf is sorted", () => {
  assert.deepEqual(keysOf(TREE), ["common.save", "nav.home", "nav.library"]);
});

test("unflatten resolves leaf-then-branch conflicts to a branch", () => {
  const tree = unflatten({ "a": "leaf", "a.b": "deeper" });
  // "a" becomes a branch holding "b"
  assert.deepEqual(tree, { a: { b: "deeper" } });
});
