import { test } from "vitest";
import assert from "node:assert/strict";
import { parse, tryParse, ICUParseError } from "./parser.ts";

test("plain literal", () => {
  assert.deepEqual(parse("hello"), [{ type: "literal", value: "hello" }]);
});

test("simple argument", () => {
  assert.deepEqual(parse("Hi {name}!"), [
    { type: "literal", value: "Hi " },
    { type: "argument", arg: "name" },
    { type: "literal", value: "!" },
  ]);
});

test("typed format with style", () => {
  const ast = parse("{price, number, ::currency/USD}");
  assert.deepEqual(ast, [
    { type: "format", arg: "price", format: "number", style: "::currency/USD" },
  ]);
});

test("number without style", () => {
  assert.deepEqual(parse("{n, number}"), [
    { type: "format", arg: "n", format: "number", style: undefined },
  ]);
});

test("plural with offset, exact arm and #", () => {
  const ast = parse(
    "{count, plural, offset:1 =0 {none} one {# item} other {# items}}",
  );
  assert.equal(ast.length, 1);
  const node = ast[0];
  assert.equal(node.type, "plural");
  if (node.type !== "plural") return;
  assert.equal(node.arg, "count");
  assert.equal(node.offset, 1);
  assert.equal(node.ordinal, false);
  assert.deepEqual(Object.keys(node.options).sort(), ["=0", "one", "other"]);
  // the "one" arm has a pound + literal
  assert.deepEqual(node.options.one, [
    { type: "pound" },
    { type: "literal", value: " item" },
  ]);
});

test("selectordinal sets the ordinal flag", () => {
  const ast = parse("{n, selectordinal, one {#st} two {#nd} few {#rd} other {#th}}");
  assert.equal(ast[0].type === "plural" && ast[0].ordinal, true);
});

test("select", () => {
  const ast = parse("{g, select, male {he} female {she} other {they}}");
  const node = ast[0];
  assert.equal(node.type, "select");
  if (node.type !== "select") return;
  assert.deepEqual(node.options.male, [{ type: "literal", value: "he" }]);
});

test("nested plural inside select arm", () => {
  const ast = parse(
    "{g, select, other {{count, plural, one {# friend} other {# friends}}}}",
  );
  const sel = ast[0];
  assert.equal(sel.type, "select");
  if (sel.type !== "select") return;
  const inner = sel.options.other[0];
  assert.equal(inner.type, "plural");
});

test("rich-text tags", () => {
  const ast = parse("read <b>this</b> now");
  assert.equal(ast.length, 3);
  assert.equal(ast[1].type, "tag");
  if (ast[1].type === "tag") {
    assert.equal(ast[1].name, "b");
    assert.deepEqual(ast[1].children, [{ type: "literal", value: "this" }]);
  }
});

test("self-closing tag", () => {
  const ast = parse("line<br/>break");
  assert.equal(ast[1].type, "tag");
  if (ast[1].type === "tag") assert.deepEqual(ast[1].children, []);
});

test("apostrophe escaping: '' is a literal apostrophe", () => {
  assert.deepEqual(parse("it''s"), [{ type: "literal", value: "it's" }]);
});

test("apostrophe quoting escapes braces", () => {
  assert.deepEqual(parse("'{'not an arg'}'"), [
    { type: "literal", value: "{not an arg}" },
  ]);
});

test("lone apostrophe before a normal char stays literal", () => {
  assert.deepEqual(parse("D'oh"), [{ type: "literal", value: "D'oh" }]);
});

test("missing 'other' arm throws", () => {
  assert.throws(() => parse("{n, plural, one {x}}"), ICUParseError);
  assert.throws(() => parse("{g, select, male {x}}"), ICUParseError);
});

test("mismatched tag throws", () => {
  assert.throws(() => parse("<b>x</i>"), ICUParseError);
});

test("unexpected stray brace throws", () => {
  assert.throws(() => parse("a } b"), ICUParseError);
});

test("tryParse returns null on bad input", () => {
  assert.equal(tryParse("{n, plural, one {x}}"), null);
  assert.notEqual(tryParse("ok {x}"), null);
});

test("i18next {{var}} double-brace is parsed as an argument", () => {
  assert.deepEqual(parse("Hi {{name}}!"), [
    { type: "literal", value: "Hi " },
    { type: "argument", arg: "name" },
    { type: "literal", value: "!" },
  ]);
});

test("{{var}} tolerates an i18next format suffix", () => {
  assert.deepEqual(parse("{{count, number}}"), [{ type: "argument", arg: "count" }]);
});

test("{{var}} allows dotted/namespaced keys", () => {
  assert.deepEqual(parse("{{user.name}}"), [{ type: "argument", arg: "user.name" }]);
});

test("single-brace ICU still works alongside double-brace", () => {
  const ast = parse("{{a}} and {b, number}");
  assert.equal(ast[0].type, "argument");
  assert.equal(ast[2].type, "format");
});

test("whitespace tolerance inside argument", () => {
  const ast = parse("{  count ,  number  ,  percent  }");
  assert.deepEqual(ast, [
    { type: "format", arg: "count", format: "number", style: "percent" },
  ]);
});
