import { test } from "vitest";
import assert from "node:assert/strict";
import { lintCatalog, collectArguments, formatLintReport } from "./lint.ts";
import { parse } from "./icu/index.ts";

test("collectArguments gathers args across nodes and arms", () => {
  const ast = parse(
    "Hi {name}, {count, plural, one {# new {kind}} other {# new {kind}s}}",
  );
  assert.deepEqual([...collectArguments(ast)].sort(), ["count", "kind", "name"]);
});

test("clean translation has no issues", () => {
  const ref = { greet: "Hi {name}", count: "{n, plural, one {# item} other {# items}}" };
  const sub = { greet: "Hola {name}", count: "{n, plural, one {# elemento} other {# elementos}}" };
  const result = lintCatalog(ref, sub, "es");
  assert.equal(result.errorCount, 0);
  assert.equal(result.issues.length, 0);
});

test("missing key is an error", () => {
  const ref = { a: "A", b: "B" };
  const sub = { a: "Ä" };
  const result = lintCatalog(ref, sub, "xx");
  const codes = result.issues.map((i) => i.code);
  assert.ok(codes.includes("missing-key"));
  assert.equal(result.errorCount, 1);
});

test("extra key is a warning by default, error under strictExtra", () => {
  const ref = { a: "A" };
  const sub = { a: "A", stale: "old" };
  assert.equal(lintCatalog(ref, sub, "xx").warningCount, 1);
  assert.equal(lintCatalog(ref, sub, "xx", { strictExtra: true }).errorCount, 1);
});

test("invalid ICU in a translation is an error", () => {
  const ref = { msg: "ok {x}" };
  const sub = { msg: "broken {n, plural, one {x}}" }; // missing 'other'
  const result = lintCatalog(ref, sub, "xx");
  assert.ok(result.issues.some((i) => i.code === "invalid-icu"));
});

test("placeholder drift is caught", () => {
  const ref = { msg: "Hi {name}" };
  const sub = { msg: "Hola {nombre}" }; // renamed the placeholder
  const result = lintCatalog(ref, sub, "es");
  assert.ok(result.issues.some((i) => i.code === "placeholder-drift"));
});

test("placeholder check can be disabled", () => {
  const ref = { msg: "Hi {name}" };
  const sub = { msg: "Hola {nombre}" };
  const result = lintCatalog(ref, sub, "es", { checkPlaceholders: false });
  assert.ok(!result.issues.some((i) => i.code === "placeholder-drift"));
});

test("formatLintReport renders a clean line + an issue block", () => {
  const clean = lintCatalog({ a: "A" }, { a: "Ä" }, "es");
  assert.match(formatLintReport(clean), /✓ es/);
  const dirty = lintCatalog({ a: "A", b: "B" }, { a: "Ä" }, "es");
  assert.match(formatLintReport(dirty), /missing-key/);
});
