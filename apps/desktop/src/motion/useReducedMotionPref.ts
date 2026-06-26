import { useSyncExternalStore } from "react";

/**
 * useReducedMotionPref — the motion system's single read of the user's
 * "reduce motion" intent.
 *
 * SEAM (Agent 6): the contract says motion CONSUMES Agent 6's
 * `useReducedMotionPref()` from `src/a11y/`. That module hasn't merged
 * yet, so this is the stub that satisfies the same contract.
 *
 * Why not framer-motion's `useReducedMotion()`: in v12 it reads the
 * preference ONCE via a `useState` initialiser with no subscription, so it
 * never updates when the OS toggle flips mid-session — while the CSS
 * `@media (prefers-reduced-motion)` layer DOES update live, leaving the two
 * layers out of sync. We subscribe to the media query via
 * `useSyncExternalStore` so the JS layer tracks the preference LIVE, and
 * every primitive (which reads this through `useMotion`) reacts the moment
 * the user turns Reduce Motion on or off.
 *
 * At integration, Agent 12 repoints this to `src/a11y/` (which may layer an
 * in-app override on top of the OS query). Call sites don't move.
 */

const QUERY = "(prefers-reduced-motion: reduce)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mq = window.matchMedia(QUERY);
  // Safari <14 only has the deprecated addListener; guard for it.
  if (mq.addEventListener) {
    mq.addEventListener("change", callback);
    return () => mq.removeEventListener("change", callback);
  }
  mq.addListener(callback);
  return () => mq.removeListener(callback);
}

function getSnapshot(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(QUERY).matches;
}

function getServerSnapshot(): boolean {
  return false;
}

export function useReducedMotionPref(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
