// Named scene transitions for the GPU compositor, pure half. A transition maps a
// normalized progress p∈[0,1] (driven by the engine over the crossfade duration)
// to the compositor's `mix` plus an optional grade modulation. These are the
// curves CSS opacity can't express — eased dissolves, an additive "bloom"
// dissolve, a brief desaturation dip mid-cut — WITHOUT ever introducing a black
// frame: every transition keeps `mix` continuous from 0→1 with both layers
// present, and any luminance dip is implemented as a grade change, never as
// drawing black. DOM-free + unit-tested; the compositor consumes the {mix, grade}.
//
// Why "dip" not "dip-to-black": §12.4 / the no-black-frame guarantee. A film cut
// that dims through the middle reads as cinematic, but actual black would violate
// the guarantee, so we dip SATURATION/EXPOSURE on the still-visible layers instead.

import { NEUTRAL_GRADE, lerpGrade, type FilmGrade } from "./grade";

export type TransitionKind = "cut" | "dissolve" | "soft-dissolve" | "bloom" | "desat-dip";

export interface TransitionFrame {
  /** crossfade position for the compositor (0 = outgoing, 1 = incoming) */
  mix: number;
  /** grade override for this frame (applied on top of the base look) */
  grade: FilmGrade;
}

const clamp01 = (v: number): number => (v < 0 ? 0 : v > 1 ? 1 : v);

/** Cubic ease-in-out — the standard "settle" curve, smooth velocity at both ends. */
export function easeInOut(p: number): number {
  const x = clamp01(p);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

/** Smoothstep — gentler than cubic, no overshoot. */
export function smoothstep(p: number): number {
  const x = clamp01(p);
  return x * x * (3 - 2 * x);
}

/** A bell that peaks at p=0.5 (1 at the midpoint, 0 at the ends) — for mid-cut
 *  modulations like the desaturation dip / bloom. */
export function midBell(p: number): number {
  const x = clamp01(p);
  return Math.sin(x * Math.PI); // 0→1→0
}

/** Resolve a transition's {mix, grade} at progress `p`. `base` is the scene's
 *  steady-state grade (so a transition modulates around the current look, not
 *  around neutral). All transitions keep `mix` monotonic 0→1 with both layers
 *  visible — never a black frame. */
export function transitionAt(kind: TransitionKind, p: number, base: FilmGrade = NEUTRAL_GRADE): TransitionFrame {
  const x = clamp01(p);
  switch (kind) {
    case "cut":
      // Instant: outgoing until the very end, then incoming. (The engine uses this
      // while scrubbing; here it's a degenerate transition.)
      return { mix: x >= 1 ? 1 : 0, grade: base };
    case "dissolve":
      return { mix: easeInOut(x), grade: base };
    case "soft-dissolve":
      return { mix: smoothstep(x), grade: base };
    case "bloom": {
      // Eased dissolve with a brief exposure lift mid-cut (gain bump), no black.
      const lift = 1 + 0.25 * midBell(x);
      const grade = lerpGrade(base, { ...base, gain: scaleRGB(base.gain, lift) }, 1);
      return { mix: easeInOut(x), grade };
    }
    case "desat-dip": {
      // Dissolve with a saturation dip at the midpoint — a "memory shimmer".
      const dip = 1 - 0.5 * midBell(x);
      const grade: FilmGrade = { ...base, saturation: base.saturation * dip };
      return { mix: smoothstep(x), grade };
    }
    default:
      return { mix: easeInOut(x), grade: base };
  }
}

/** Sample a transition into N evenly-spaced frames — handy for previews + tests. */
export function sampleTransition(kind: TransitionKind, frames: number, base?: FilmGrade): TransitionFrame[] {
  const n = Math.max(2, Math.floor(frames));
  const out: TransitionFrame[] = [];
  for (let i = 0; i < n; i++) out.push(transitionAt(kind, i / (n - 1), base));
  return out;
}

function scaleRGB(rgb: readonly [number, number, number], k: number): [number, number, number] {
  return [rgb[0] * k, rgb[1] * k, rgb[2] * k];
}
