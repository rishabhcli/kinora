import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  useReducedMotionPref,
  setReducedMotionOverride,
  getReducedMotionSnapshot,
  getReducedMotionOverride,
} from "./useReducedMotionPref";

// A single shared MediaQueryList-like mock whose `matches` is mutable, so the
// module's fresh `matchMedia(...).matches` reads and the `change` subscription
// observe the same source.
function installMatchMedia(initial: boolean) {
  const listeners = new Set<(e: { matches: boolean }) => void>();
  const mql = {
    matches: initial,
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addEventListener: (_t: string, cb: (e: { matches: boolean }) => void) => listeners.add(cb),
    removeEventListener: (_t: string, cb: (e: { matches: boolean }) => void) => listeners.delete(cb),
    addListener: (cb: (e: { matches: boolean }) => void) => listeners.add(cb),
    removeListener: (cb: (e: { matches: boolean }) => void) => listeners.delete(cb),
    dispatchEvent: () => true,
  };
  window.matchMedia = vi.fn(() => mql) as unknown as typeof window.matchMedia;
  return {
    setOsReduced(v: boolean) {
      mql.matches = v;
      listeners.forEach((l) => l({ matches: v }));
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  installMatchMedia(false);
  setReducedMotionOverride(null); // reset to "follow OS"
});

describe("useReducedMotionPref", () => {
  it("is false when the OS has no preference and there is no override", () => {
    const { result } = renderHook(() => useReducedMotionPref());
    expect(result.current).toBe(false);
  });

  it("is true when the OS prefers reduced motion", () => {
    installMatchMedia(true);
    const { result } = renderHook(() => useReducedMotionPref());
    expect(result.current).toBe(true);
  });

  it("an in-app override of true forces reduced motion even when the OS is off", () => {
    const { result } = renderHook(() => useReducedMotionPref());
    expect(result.current).toBe(false);
    act(() => setReducedMotionOverride(true));
    expect(result.current).toBe(true);
  });

  it("an in-app override of false forces full motion even when the OS is on", () => {
    installMatchMedia(true);
    const { result } = renderHook(() => useReducedMotionPref());
    expect(result.current).toBe(true);
    act(() => setReducedMotionOverride(false));
    expect(result.current).toBe(false);
  });

  it("clearing the override (null) follows the OS again", () => {
    installMatchMedia(true);
    const { result } = renderHook(() => useReducedMotionPref());
    act(() => setReducedMotionOverride(false));
    expect(result.current).toBe(false);
    act(() => setReducedMotionOverride(null));
    expect(result.current).toBe(true);
  });

  it("re-renders when the OS preference changes at runtime", () => {
    const mm = installMatchMedia(false);
    const { result } = renderHook(() => useReducedMotionPref());
    expect(result.current).toBe(false);
    act(() => mm.setOsReduced(true));
    expect(result.current).toBe(true);
  });
});

describe("imperative API", () => {
  it("getReducedMotionSnapshot reflects the current resolved value", () => {
    setReducedMotionOverride(true);
    expect(getReducedMotionSnapshot()).toBe(true);
  });

  it("persists the override and exposes it via getReducedMotionOverride", () => {
    setReducedMotionOverride(true);
    expect(getReducedMotionOverride()).toBe(true);
    expect(localStorage.getItem("kinora.reduceMotion")).toBe("reduce");
  });
});
