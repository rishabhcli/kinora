import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { createMediaPref } from "./mediaPref";

function installMatchMedia(initial: boolean) {
  const listeners = new Set<(e: { matches: boolean }) => void>();
  const mql = {
    matches: initial,
    media: "",
    onchange: null,
    addEventListener: (_t: string, cb: (e: { matches: boolean }) => void) => listeners.add(cb),
    removeEventListener: (_t: string, cb: (e: { matches: boolean }) => void) => listeners.delete(cb),
    addListener: (cb: (e: { matches: boolean }) => void) => listeners.add(cb),
    removeListener: (cb: (e: { matches: boolean }) => void) => listeners.delete(cb),
    dispatchEvent: () => true,
  };
  window.matchMedia = vi.fn(() => mql) as unknown as typeof window.matchMedia;
  return {
    setOs(v: boolean) {
      mql.matches = v;
      listeners.forEach((l) => l({ matches: v }));
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  installMatchMedia(false);
});

describe("createMediaPref", () => {
  it("resolves to the OS media state when there is no override", () => {
    installMatchMedia(true);
    const pref = createMediaPref({ media: "(prefers-contrast: more)", storageKey: "k.hc" });
    expect(pref.getSnapshot()).toBe(true);
  });

  it("an override wins over the OS state", () => {
    installMatchMedia(true);
    const pref = createMediaPref({ media: "(x)", storageKey: "k.x" });
    pref.setOverride(false);
    expect(pref.getSnapshot()).toBe(false);
    pref.setOverride(true);
    expect(pref.getSnapshot()).toBe(true);
    pref.setOverride(null);
    expect(pref.getSnapshot()).toBe(true); // back to OS (which is true)
  });

  it("persists the override under the given key with custom on/off tokens", () => {
    const pref = createMediaPref({
      media: "(x)",
      storageKey: "kinora.reduceMotion",
      onValue: "reduce",
      offValue: "full",
    });
    pref.setOverride(true);
    expect(localStorage.getItem("kinora.reduceMotion")).toBe("reduce");
    pref.setOverride(false);
    expect(localStorage.getItem("kinora.reduceMotion")).toBe("full");
    pref.setOverride(null);
    expect(localStorage.getItem("kinora.reduceMotion")).toBeNull();
  });

  it("loads a previously-persisted override on creation", () => {
    localStorage.setItem("k.persist", "on");
    const pref = createMediaPref({ media: "(x)", storageKey: "k.persist" });
    expect(pref.getOverride()).toBe(true);
  });

  it("the hook re-renders on OS change and on setOverride", () => {
    const mm = installMatchMedia(false);
    const pref = createMediaPref({ media: "(x)", storageKey: "k.h" });
    const { result } = renderHook(() => pref.use());
    expect(result.current).toBe(false);
    act(() => mm.setOs(true));
    expect(result.current).toBe(true);
    act(() => pref.setOverride(false));
    expect(result.current).toBe(false);
  });
});
