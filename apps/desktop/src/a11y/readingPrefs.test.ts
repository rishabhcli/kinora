import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  normalizeReadingPrefs,
  resolveEffectiveTheme,
  useReadingPrefs,
  DEFAULT_READING_PREFS,
  READING_FONTS,
} from "./readingPrefs";

beforeEach(() => localStorage.clear());

describe("normalizeReadingPrefs", () => {
  it("fills defaults for empty/null input", () => {
    expect(normalizeReadingPrefs(null)).toEqual(DEFAULT_READING_PREFS);
    expect(normalizeReadingPrefs({})).toEqual(DEFAULT_READING_PREFS);
  });

  it("clamps numeric fields to their reading-comfort ranges", () => {
    const p = normalizeReadingPrefs({ fontScale: 9, measure: 5, leading: 0, brightness: 2, ttsRate: 99 });
    expect(p.fontScale).toBe(1.6);
    expect(p.measure).toBe(44);
    expect(p.leading).toBe(1.3);
    expect(p.brightness).toBe(1);
    expect(p.ttsRate).toBe(2);
  });

  it("rejects invalid enum values back to defaults", () => {
    const p = normalizeReadingPrefs({
      theme: "neon" as never,
      fontFamily: "wingdings" as never,
      spacing: "huge" as never,
      readingMode: "spiral" as never,
    });
    expect(p.theme).toBe(DEFAULT_READING_PREFS.theme);
    expect(p.fontFamily).toBe(DEFAULT_READING_PREFS.fontFamily);
    expect(p.spacing).toBe(DEFAULT_READING_PREFS.spacing);
    expect(p.readingMode).toBe("scroll");
  });

  it("migrates the legacy shape (no fontFamily/brightness/tts) by adding defaults, keeping set values", () => {
    const legacy = { theme: "sepia", autoNight: true, fontScale: 1.2, leading: 1.9, measure: 70, spacing: "relaxed" };
    const p = normalizeReadingPrefs(legacy as never);
    expect(p.theme).toBe("sepia");
    expect(p.fontScale).toBe(1.2);
    expect(p.fontFamily).toBe(DEFAULT_READING_PREFS.fontFamily); // added
    expect(p.brightness).toBe(DEFAULT_READING_PREFS.brightness); // added
    expect(p.ttsVoiceURI).toBeNull();
  });

  it("accepts a dyslexia font family", () => {
    expect(normalizeReadingPrefs({ fontFamily: "dyslexic" }).fontFamily).toBe("dyslexic");
    expect(READING_FONTS.dyslexic.className).toBe("reading-font-dyslexic");
  });
});

describe("resolveEffectiveTheme", () => {
  it("forces Night between 19:00 and 07:00 when autoNight is on", () => {
    const base = normalizeReadingPrefs({ theme: "sepia", autoNight: true });
    expect(resolveEffectiveTheme(base, 22)).toBe("night");
    expect(resolveEffectiveTheme(base, 3)).toBe("night");
    expect(resolveEffectiveTheme(base, 12)).toBe("sepia");
  });

  it("returns the chosen theme when autoNight is off", () => {
    const base = normalizeReadingPrefs({ theme: "paper", autoNight: false });
    expect(resolveEffectiveTheme(base, 22)).toBe("paper");
  });
});

describe("useReadingPrefs", () => {
  it("loads defaults, updates, and persists to localStorage", () => {
    const { result } = renderHook(() => useReadingPrefs());
    expect(result.current.prefs.fontFamily).toBe(DEFAULT_READING_PREFS.fontFamily);
    act(() => result.current.update({ fontFamily: "dyslexic", fontScale: 1.3 }));
    expect(result.current.prefs.fontFamily).toBe("dyslexic");
    expect(JSON.parse(localStorage.getItem("kinora.readingPrefs")!).fontFamily).toBe("dyslexic");
  });

  it("exposes the effectiveTheme honouring autoNight", () => {
    const { result } = renderHook(() => useReadingPrefs());
    act(() => result.current.update({ theme: "sepia", autoNight: false }));
    expect(result.current.effectiveTheme).toBe("sepia");
  });
});
