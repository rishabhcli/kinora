/**
 * springs.ts — the numeric heart of the Kinora motion system.
 *
 * These are the framer-motion twins of the CSS tokens in
 * `src/styles/motion.css`. The whole app animates with the same small,
 * opinionated set of springs so it feels like one instrument rather than
 * a dozen ad-hoc easings.
 *
 * The physics model (designer-readable):
 *   - `gentle`    — low bounce, medium length. The default for reveals,
 *                   hovers, list settles. Calm, never springy.
 *   - `snappy`    — short and a little eager. UI that should feel instant
 *                   and responsive: pills, toggles, dock magnification.
 *   - `cinematic` — long, heavy, almost no bounce. The headline moments
 *                   (book open/close travel). Reads as "expensive".
 *
 * We express springs in framer-motion's {duration, bounce} form (not
 * stiffness/damping) because it is what designers reason about AND it is
 * the only spring form that scales cleanly under a global speed knob.
 *
 * Everything here is a pure function or a frozen constant — trivially
 * testable, no React, no side effects.
 */

export type SpringPreset = "gentle" | "snappy" | "cinematic";

/** A cubic-bezier easing (framer-motion's `Easing` tuple form). */
export type CubicBezier = [number, number, number, number];

/** Named easings we use (a subset of framer-motion's `Easing`). */
export type NamedEase = "linear" | "easeIn" | "easeOut" | "easeInOut";

/** A framer-motion spring transition in duration/bounce form. */
export interface SpringTransition {
  type: "spring";
  duration: number;
  bounce: number;
}

/** A framer-motion tween transition. */
export interface TweenTransition {
  type: "tween";
  duration: number;
  ease: CubicBezier | NamedEase;
}

export type MotionTransition = SpringTransition | TweenTransition;

/* — Spring presets (the base, at speed = 1, motion enabled) — */
export const SPRINGS: Readonly<Record<SpringPreset, SpringTransition>> = Object.freeze({
  gentle: { type: "spring", duration: 0.55, bounce: 0.18 },
  snappy: { type: "spring", duration: 0.38, bounce: 0.3 },
  cinematic: { type: "spring", duration: 0.9, bounce: 0.08 },
});

/* — Easing curves — the numeric twins of the --mo-ease-* CSS vars.
   Mutable cubic-bezier tuples so they satisfy framer-motion's `Easing`. */
export const EASE = Object.freeze({
  /** The Kinora signature settle. Matches --mo-ease-standard. */
  standard: [0.22, 1, 0.36, 1] as CubicBezier,
  /** Expressive entrances. Matches --mo-ease-emphasized. */
  emphasized: [0.16, 1, 0.3, 1] as CubicBezier,
  /** Leave fast. Matches --mo-ease-exit. */
  exit: [0.4, 0, 1, 1] as CubicBezier,
  /** A touch of overshoot. Matches --mo-ease-spring. */
  spring: [0.34, 1.56, 0.64, 1] as CubicBezier,
  /** Linear-ish UI glide. Matches --mo-ease-glide. */
  glide: [0.4, 0, 0.2, 1] as CubicBezier,
});

export type EaseToken = keyof typeof EASE;

/* — Duration scale (seconds) — the twins of the --mo-dur-* CSS vars — */
export const DURATION = Object.freeze({
  instant: 0.12,
  fast: 0.18,
  base: 0.32,
  slow: 0.55,
  cinematic: 0.9,
});

export type DurationToken = keyof typeof DURATION;

/**
 * Scale a transition by the global speed multiplier and collapse it for
 * reduced motion. Pure: same inputs → same output, no clamping surprises.
 *
 * @param t       the base transition (spring or tween)
 * @param speed   global multiplier; 1 = normal, 2 = twice as fast.
 *                Larger = faster, so durations divide by it.
 * @param reduced when true, returns a near-instant tween (motion off)
 */
export function scaleTransition(
  t: MotionTransition,
  speed = 1,
  reduced = false,
): MotionTransition {
  if (reduced) return { type: "tween", duration: 0, ease: "linear" };
  const safeSpeed = speed > 0 ? speed : 1;
  return { ...t, duration: t.duration / safeSpeed };
}

/**
 * Resolve a spring preset into a concrete, speed/reduced-aware transition.
 * The ergonomic entry point most call sites use (via `useMotion`).
 */
export function spring(
  preset: SpringPreset = "gentle",
  speed = 1,
  reduced = false,
): MotionTransition {
  return scaleTransition(SPRINGS[preset], speed, reduced);
}

/**
 * Resolve a tween from the duration + ease scales, speed/reduced-aware.
 */
export function tween(
  durationToken: DurationToken = "base",
  easeToken: EaseToken = "standard",
  speed = 1,
  reduced = false,
): TweenTransition {
  if (reduced) return { type: "tween", duration: 0, ease: "linear" };
  const safeSpeed = speed > 0 ? speed : 1;
  return {
    type: "tween",
    duration: DURATION[durationToken] / safeSpeed,
    ease: EASE[easeToken],
  };
}
