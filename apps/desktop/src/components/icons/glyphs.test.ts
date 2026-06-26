import { test } from "node:test";
import assert from "node:assert/strict";
import { GLYPHS, ICON_NAMES } from "./glyphs.ts";

test("registry exposes a name list matching its keys, with no dupes", () => {
  const keys = Object.keys(GLYPHS);
  assert.equal(ICON_NAMES.length, keys.length);
  assert.equal(new Set(ICON_NAMES).size, ICON_NAMES.length, "ICON_NAMES has duplicates");
  for (const name of ICON_NAMES) {
    assert.ok(name in GLYPHS, `${name} listed but missing a glyph`);
  }
});

test("every glyph has at least one layer with real path data", () => {
  for (const name of ICON_NAMES) {
    const g = GLYPHS[name];
    assert.ok(g.layers.length >= 1, `${name} has no layers`);
    for (const layer of g.layers) {
      assert.equal(typeof layer.d, "string");
      assert.ok(layer.d.trim().length > 0, `${name} has an empty path`);
      assert.match(layer.d.trim(), /^[Mm]/, `${name} path should start with a moveto`);
    }
  }
});

test("viewBox, when set, is a 4-number box", () => {
  for (const name of ICON_NAMES) {
    const vb = GLYPHS[name].viewBox;
    if (vb !== undefined) {
      assert.match(vb, /^-?\d+(\.\d+)?( -?\d+(\.\d+)?){3}$/, `${name} bad viewBox: ${vb}`);
    }
  }
});

test(".fill names render a solid (filled) primary shape", () => {
  for (const name of ICON_NAMES) {
    if (name.endsWith(".fill")) {
      assert.ok(
        GLYPHS[name].layers.some((l) => l.fill === true),
        `${name} is a .fill variant but has no filled layer`,
      );
    }
  }
});

test("acceptance: book.fill, house.fill, gearshape and magnifyingglass exist", () => {
  for (const name of ["book.fill", "house.fill", "gearshape", "magnifyingglass"] as const) {
    assert.ok(GLYPHS[name], `${name} missing`);
  }
});
