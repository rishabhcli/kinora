// Pure helpers behind <Icon> — no React, no JSX, so they're unit-testable with
// `node --test` directly. Keep all rendering decisions that aren't markup here.
import type { LayerRole, SymbolWeight } from "./types";

/** Authored stroke weights on the 24-unit grid (regular ≈ the app's prior 1.6–1.7). */
const STROKE_AT_24: Record<SymbolWeight, number> = {
  ultralight: 0.9,
  light: 1.25,
  regular: 1.6,
  medium: 1.95,
  semibold: 2.3,
  bold: 2.7,
};

/**
 * Stroke width in viewBox units for a glyph rendered at `size` px.
 *
 * SF Symbols keep a roughly constant *optical* (on-screen px) weight as a symbol
 * grows, which means the stroke gets thinner relative to the glyph's own grid.
 * We author on a 24-grid, so we scale by 24/size: a 48px icon strokes at half the
 * grid-units of a 24px one, landing at the same ~px thickness on screen.
 */
export function weightToStrokeWidth(weight: SymbolWeight, size = 24): number {
  const base = STROKE_AT_24[weight];
  const scaled = base * (24 / size);
  // Round to keep SVG output tidy without collapsing thin strokes to 0.
  return Math.round(scaled * 1000) / 1000;
}

/** Opacity for a layer at a given hierarchical depth. */
export function hierarchicalOpacity(role: LayerRole = "primary"): number {
  switch (role) {
    case "primary":
      return 1;
    case "secondary":
      return 0.55;
    case "tertiary":
      return 0.3;
  }
}

export interface IconA11y {
  role?: "img";
  "aria-label"?: string;
  "aria-hidden"?: true;
  /** SVGs aren't focusable for us; pinning this off also tames legacy IE/Edge. */
  focusable: false;
}

/**
 * An icon with a `title` is meaningful → expose it as `role="img"` with a label.
 * Without one it's decorative → hide it from assistive tech entirely. (Agent 6's
 * a11y checklist: every icon-only button still needs its *own* label on the button.)
 */
export function resolveAccessibility(title?: string): IconA11y {
  if (title && title.trim().length > 0) {
    return { role: "img", "aria-label": title, focusable: false };
  }
  return { "aria-hidden": true, focusable: false };
}
