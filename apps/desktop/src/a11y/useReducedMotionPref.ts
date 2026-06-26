import { createMediaPref, type PrefOverride } from "./mediaPref";

// The single source of truth for reduced motion across the whole app.
// Resolved value = in-app override (if set) ELSE the OS `prefers-reduced-motion`.
// Agent 4's motion system and every animating component must consume this hook
// (not framer-motion's `useReducedMotion()` directly) so the in-app toggle works.

export type ReduceMotionOverride = PrefOverride;

const pref = createMediaPref({
  media: "(prefers-reduced-motion: reduce)",
  storageKey: "kinora.reduceMotion",
  onValue: "reduce",
  offValue: "full",
});

export function useReducedMotionPref(): boolean {
  return pref.use();
}

/** Non-hook read for imperative code (animation helpers, one-off branches). */
export function getReducedMotionSnapshot(): boolean {
  return pref.getSnapshot();
}

export function getReducedMotionOverride(): ReduceMotionOverride {
  return pref.getOverride();
}

/** Set the in-app override (null follows the OS). Notifies all consumers. */
export function setReducedMotionOverride(value: ReduceMotionOverride): void {
  pref.setOverride(value);
}
