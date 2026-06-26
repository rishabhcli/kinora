import { test } from "node:test";
import assert from "node:assert/strict";
import { overrideCss, resolveAppearanceSettings } from "./appearance.ts";
import { SETTINGS_DEFAULTS } from "./settings.ts";

test("overrideCss targets all three appearance classes", () => {
  const css = overrideCss();
  assert.match(css, /kinora-reduce-motion/);
  assert.match(css, /kinora-reduce-transparency/);
  assert.match(css, /kinora-increase-contrast/);
  // reduce-motion must actually zero animation/transition durations
  assert.match(css, /animation-duration/);
  assert.match(css, /transition-duration/);
});

test("resolveAppearanceSettings: explicit on/off ignore the (absent) system query", () => {
  const r = resolveAppearanceSettings({
    ...SETTINGS_DEFAULTS,
    reduceMotion: "on",
    reduceTransparency: "off",
    increaseContrast: "on",
  });
  assert.equal(r.reduceMotion, true);
  assert.equal(r.reduceTransparency, false);
  assert.equal(r.increaseContrast, true);
});

test("resolveAppearanceSettings: 'system' with no matchMedia resolves false (safe default)", () => {
  // node has no matchMedia → system prefs read as not-set.
  const r = resolveAppearanceSettings(SETTINGS_DEFAULTS);
  assert.equal(r.reduceMotion, false);
  assert.equal(r.reduceTransparency, false);
  assert.equal(r.increaseContrast, false);
});
