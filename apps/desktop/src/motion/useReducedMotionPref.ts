import { useReducedMotion } from "framer-motion";

/**
 * useReducedMotionPref — the motion system's single read of the user's
 * "reduce motion" intent.
 *
 * SEAM (Agent 6): the contract says motion CONSUMES Agent 6's
 * `useReducedMotionPref()` from `src/a11y/`. That module hasn't merged
 * yet, so this is the stub that satisfies the same contract:
 *   - returns a definite boolean (never null),
 *   - reads the OS `prefers-reduced-motion` media query.
 *
 * At integration, Agent 12 re-points this to `src/a11y/` (which may add
 * an in-app override on top of the OS query). Every motion primitive
 * imports the preference THROUGH `useMotion()`, so swapping the source is
 * a one-line change here — call sites don't move.
 */
export function useReducedMotionPref(): boolean {
  // framer-motion returns `null` before it has observed the media query.
  return useReducedMotion() ?? false;
}
