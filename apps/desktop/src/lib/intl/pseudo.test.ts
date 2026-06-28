import { test } from "vitest";
import assert from "node:assert/strict";
import { pseudoLocalize, pseudoLocalizeCatalog } from "./pseudo.ts";
import { formatMessage } from "./icu/index.ts";

test("brackets wrap the message", () => {
  const out = pseudoLocalize("Hi", { expand: 1, accent: false });
  assert.equal(out, "⟦Hi⟧");
});

test("accents Latin letters", () => {
  const out = pseudoLocalize("abc", { expand: 1, brackets: false });
  assert.equal(out, "áƀç");
});

test("placeholders are NOT touched", () => {
  const out = pseudoLocalize("Hi {name}!", { expand: 1, brackets: false });
  assert.ok(out.includes("{name}"));
  assert.ok(!out.includes("{ñámé}"));
});

test("plural submessages stay intact and result is still valid ICU", () => {
  const src = "{n, plural, one {# message} other {# messages}}";
  const pseudo = pseudoLocalize(src, { expand: 1 });
  // The whole plural is a placeholder span → untouched (except outer brackets).
  assert.ok(pseudo.includes("{n, plural, one {# message} other {# messages}}"));
  // Still parses + evaluates.
  const inner = pseudo.replace(/^⟦/, "").replace(/⟧$/, "");
  assert.equal(formatMessage(inner, "en", { n: 2 }), "2 messages");
});

test("rich-text tags are preserved", () => {
  const out = pseudoLocalize("read <b>this</b>", { expand: 1, brackets: false });
  assert.ok(out.includes("<b>"));
  assert.ok(out.includes("</b>"));
});

test("ICU-quoted spans pass through verbatim", () => {
  const out = pseudoLocalize("a '{' b", { expand: 1, brackets: false });
  assert.ok(out.includes("'{'"));
});

test("expansion lengthens the literal", () => {
  const base = pseudoLocalize("aaaa eeee", { expand: 1, brackets: false, accent: false });
  const expanded = pseudoLocalize("aaaa eeee", { expand: 2, brackets: false, accent: false });
  assert.ok(expanded.length > base.length);
});

test("pseudo-localized ICU still evaluates with correct plural selection", () => {
  const src = "{count, plural, one {# item} other {# items}}";
  const pseudo = pseudoLocalize(src, { expand: 1 });
  const inner = pseudo.replace(/^⟦/, "").replace(/⟧$/, "");
  // plural arm logic survives pseudo (placeholders untouched)
  assert.equal(formatMessage(inner, "en", { count: 1 }), "1 item");
  assert.equal(formatMessage(inner, "en", { count: 4 }), "4 items");
});

test("pseudo preserves i18next {{double}} placeholders", () => {
  const out = pseudoLocalize("Hello {{name}}!", { expand: 1, brackets: false });
  assert.ok(out.includes("{{name}}"));
});

test("catalog tree is deep-transformed, structure preserved", () => {
  const tree = {
    nav: { home: "Home", library: "Library" },
    msg: "Hi {name}",
  };
  const pseudo = pseudoLocalizeCatalog(tree, { expand: 1 });
  assert.equal(typeof pseudo.nav, "object");
  assert.ok(pseudo.nav.home.startsWith("⟦"));
  assert.ok(pseudo.msg.includes("{name}"));
});
