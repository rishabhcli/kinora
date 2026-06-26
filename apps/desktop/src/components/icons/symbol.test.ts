import { test } from "node:test";
import assert from "node:assert/strict";
import {
  weightToStrokeWidth,
  hierarchicalOpacity,
  resolveAccessibility,
} from "./symbol.ts";
import type { SymbolWeight } from "./types.ts";

test("weightToStrokeWidth: regular sits near the app's 1.6–1.7 stroke", () => {
  const w = weightToStrokeWidth("regular");
  assert.ok(w >= 1.5 && w <= 1.8, `regular=${w} should be ~1.6`);
});

test("weightToStrokeWidth: strictly increases ultralight→bold", () => {
  const order: SymbolWeight[] = [
    "ultralight",
    "light",
    "regular",
    "medium",
    "semibold",
    "bold",
  ];
  const widths = order.map((w) => weightToStrokeWidth(w));
  for (let i = 1; i < widths.length; i++) {
    assert.ok(widths[i] > widths[i - 1], `${order[i]} must be heavier than ${order[i - 1]}`);
  }
});

test("weightToStrokeWidth: scales with the rendered size (a 32px glyph isn't a fat 24px one)", () => {
  // Stroke is authored on a 24 grid; at larger sizes the optical weight should
  // stay constant, so the returned width scales down relative to viewBox units.
  const at24 = weightToStrokeWidth("regular", 24);
  const at48 = weightToStrokeWidth("regular", 48);
  assert.ok(at48 < at24, "stroke width (in viewBox units) shrinks as render size grows");
});

test("hierarchicalOpacity: primary is opaque, depth fades", () => {
  assert.equal(hierarchicalOpacity("primary"), 1);
  assert.ok(hierarchicalOpacity("secondary") < 1);
  assert.ok(hierarchicalOpacity("tertiary") < hierarchicalOpacity("secondary"));
});

test("resolveAccessibility: a title makes the glyph an img with a label", () => {
  const a = resolveAccessibility("Search");
  assert.equal(a.role, "img");
  assert.equal(a["aria-label"], "Search");
  assert.notEqual(a["aria-hidden"], true);
});

test("resolveAccessibility: no/blank title hides it from the a11y tree", () => {
  for (const t of [undefined, "", "   "]) {
    const a = resolveAccessibility(t);
    assert.equal(a["aria-hidden"], true);
    assert.equal(a.focusable, false);
    assert.equal(a.role, undefined);
  }
});
