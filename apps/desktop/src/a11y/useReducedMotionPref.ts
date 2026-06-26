import { useSyncExternalStore } from "react";

// The single source of truth for reduced motion across the whole app.
// Resolved value = in-app override (if set) ELSE the OS `prefers-reduced-motion`.
// Agent 4's motion system and every animating component must consume this hook
// (not framer-motion's `useReducedMotion()` directly) so the in-app toggle works.

const MEDIA = "(prefers-reduced-motion: reduce)";
const STORAGE_KEY = "kinora.reduceMotion"; // "reduce" | "full" | (absent = follow OS)

/** null = follow the OS; true = always reduce; false = always full motion. */
export type ReduceMotionOverride = boolean | null;

function loadOverride(): ReduceMotionOverride {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "reduce") return true;
    if (v === "full") return false;
  } catch {
    /* storage blocked */
  }
  return null;
}

function persistOverride(v: ReduceMotionOverride): void {
  try {
    if (v === null) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, v ? "reduce" : "full");
  } catch {
    /* storage blocked */
  }
}

let override: ReduceMotionOverride = loadOverride();
const listeners = new Set<() => void>();

function osPrefersReduced(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia(MEDIA).matches;
}

function resolve(): boolean {
  return override === null ? osPrefersReduced() : override;
}

function subscribe(callback: () => void): () => void {
  listeners.add(callback);
  let mql: MediaQueryList | undefined;
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    mql = window.matchMedia(MEDIA);
    // Safari <14 only has the deprecated addListener.
    if (mql.addEventListener) mql.addEventListener("change", callback);
    else mql.addListener(callback);
  }
  return () => {
    listeners.delete(callback);
    if (mql) {
      if (mql.removeEventListener) mql.removeEventListener("change", callback);
      else mql.removeListener(callback);
    }
  };
}

export function useReducedMotionPref(): boolean {
  return useSyncExternalStore(subscribe, resolve, resolve);
}

/** Non-hook read for imperative code (animation helpers, one-off branches). */
export function getReducedMotionSnapshot(): boolean {
  return resolve();
}

export function getReducedMotionOverride(): ReduceMotionOverride {
  return override;
}

/** Set the in-app override (null follows the OS). Notifies all consumers. */
export function setReducedMotionOverride(value: ReduceMotionOverride): void {
  override = value;
  persistOverride(value);
  listeners.forEach((l) => l());
}
