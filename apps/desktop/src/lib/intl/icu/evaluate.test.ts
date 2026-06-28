import { test } from "vitest";
import assert from "node:assert/strict";
import { formatMessage, formatMessageToParts } from "./index.ts";

test("literal passthrough", () => {
  assert.equal(formatMessage("hello", "en"), "hello");
});

test("argument interpolation", () => {
  assert.equal(formatMessage("Hi {name}!", "en", { name: "Ada" }), "Hi Ada!");
});

test("missing argument renders the key in dev mode", () => {
  assert.equal(formatMessage("Hi {name}", "en"), "Hi {name}");
  assert.equal(formatMessage("Hi {name}", "en", {}, "empty"), "Hi ");
});

test("number format respects locale grouping", () => {
  assert.equal(formatMessage("{n, number}", "en-US", { n: 1234.5 }), "1,234.5");
  assert.equal(formatMessage("{n, number}", "de-DE", { n: 1234.5 }), "1.234,5");
});

test("number integer style rounds (ICU semantics)", () => {
  assert.equal(formatMessage("{n, number, integer}", "en-US", { n: 9.9 }), "10");
  assert.equal(formatMessage("{n, number, integer}", "en-US", { n: 9.4 }), "9");
});

test("currency skeleton", () => {
  assert.equal(
    formatMessage("{p, number, ::currency/USD}", "en-US", { p: 1234.5 }),
    "$1,234.50",
  );
});

test("compact skeleton", () => {
  assert.equal(
    formatMessage("{n, number, ::compact-short}", "en-US", { n: 12000 }),
    "12K",
  );
});

test("percent format", () => {
  assert.equal(formatMessage("{r, number, percent}", "en-US", { r: 0.5 }), "50%");
});

test("date format with style", () => {
  const d = new Date(Date.UTC(2026, 5, 28));
  const out = formatMessage("{when, date, long}", "en-US", { when: d });
  assert.match(out, /June (27|28), 2026/);
});

test("plural picks the right arm and substitutes #", () => {
  const msg = "{count, plural, =0 {no items} one {# item} other {# items}}";
  assert.equal(formatMessage(msg, "en", { count: 0 }), "no items");
  assert.equal(formatMessage(msg, "en", { count: 1 }), "1 item");
  assert.equal(formatMessage(msg, "en", { count: 5 }), "5 items");
});

test("plural offset adjusts # and category", () => {
  const msg =
    "{count, plural, offset:1 =0 {nobody} one {you and # other} other {you and # others}}";
  // count:0 → exact =0 arm; count:1 → adjusted 0 → English "other"; count:3 → adjusted 2 → "other"
  assert.equal(formatMessage(msg, "en", { count: 0 }), "nobody");
  assert.equal(formatMessage(msg, "en", { count: 1 }), "you and 0 others");
  assert.equal(formatMessage(msg, "en", { count: 2 }), "you and 1 other");
  assert.equal(formatMessage(msg, "en", { count: 3 }), "you and 2 others");
});

test("selectordinal", () => {
  const msg = "{n, selectordinal, one {#st} two {#nd} few {#rd} other {#th}}";
  assert.equal(formatMessage(msg, "en", { n: 1 }), "1st");
  assert.equal(formatMessage(msg, "en", { n: 2 }), "2nd");
  assert.equal(formatMessage(msg, "en", { n: 3 }), "3rd");
  assert.equal(formatMessage(msg, "en", { n: 4 }), "4th");
  assert.equal(formatMessage(msg, "en", { n: 11 }), "11th");
});

test("select with fallback to other", () => {
  const msg = "{g, select, male {he} female {she} other {they}}";
  assert.equal(formatMessage(msg, "en", { g: "male" }), "he");
  assert.equal(formatMessage(msg, "en", { g: "nonbinary" }), "they");
});

test("Polish plural categories drive arm selection", () => {
  const msg = "{n, plural, one {# plik} few {# pliki} many {# plików} other {# pliku}}";
  assert.equal(formatMessage(msg, "pl", { n: 1 }), "1 plik");
  assert.equal(formatMessage(msg, "pl", { n: 2 }), "2 pliki");
  assert.equal(formatMessage(msg, "pl", { n: 5 }), "5 plików");
});

test("nested select containing a plural", () => {
  const msg =
    "{g, select, female {She has {n, plural, one {# cat} other {# cats}}} other {They have {n, plural, one {# cat} other {# cats}}}}";
  assert.equal(formatMessage(msg, "en", { g: "female", n: 1 }), "She has 1 cat");
  assert.equal(formatMessage(msg, "en", { g: "x", n: 3 }), "They have 3 cats");
});

test("rich-text parts preserve tags", () => {
  const parts = formatMessageToParts("Read <b>{title}</b> now", "en", { title: "Dune" });
  assert.equal(parts.length, 3);
  assert.deepEqual(parts[0], { type: "text", value: "Read " });
  assert.equal(parts[1].type, "tag");
  if (parts[1].type === "tag") {
    assert.equal(parts[1].name, "b");
    assert.deepEqual(parts[1].children, [{ type: "text", value: "Dune" }]);
  }
});

test("string evaluator flattens tags", () => {
  assert.equal(formatMessage("Read <b>{x}</b>", "en", { x: "this" }), "Read this");
});

test("bare # outside a plural is a literal", () => {
  assert.equal(formatMessage("issue #", "en"), "issue #");
});

test("i18next {{var}} interpolation evaluates", () => {
  assert.equal(
    formatMessage("Continue with {{provider}}", "en", { provider: "Apple" }),
    "Continue with Apple",
  );
  assert.equal(
    formatMessage("{{seconds}}s ahead", "en", { seconds: 12 }),
    "12s ahead",
  );
});
