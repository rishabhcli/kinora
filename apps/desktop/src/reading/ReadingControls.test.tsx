import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ReadingControls } from "./ReadingControls";
import { DEFAULT_READING_PREFS, type ReadingPrefs } from "@/a11y/readingPrefs";

// jsdom has no speechSynthesis; the panel must degrade gracefully.
beforeEach(() => {
  localStorage.clear();
  delete (window as unknown as { matchMedia?: unknown }).matchMedia;
});

function renderControls(over: Partial<ReadingPrefs> = {}) {
  const onChange = vi.fn();
  const prefs = { ...DEFAULT_READING_PREFS, ...over };
  render(<ReadingControls prefs={prefs} onChange={onChange} />);
  return { onChange };
}

describe("ReadingControls", () => {
  it("is a labelled settings group", () => {
    renderControls();
    expect(screen.getByRole("group", { name: /reading settings/i })).toBeInTheDocument();
  });

  it("selecting a theme reports the change", () => {
    const { onChange } = renderControls();
    fireEvent.click(screen.getByRole("radio", { name: /sepia/i }));
    expect(onChange).toHaveBeenCalledWith({ theme: "sepia" });
  });

  it("offers the dyslexia font and reports the change", () => {
    const { onChange } = renderControls();
    fireEvent.click(screen.getByRole("radio", { name: /dyslexia/i }));
    expect(onChange).toHaveBeenCalledWith({ fontFamily: "dyslexic" });
  });

  it("text size is an accessible slider that reports a clamped value", () => {
    const { onChange } = renderControls({ fontScale: 1 });
    const slider = screen.getByRole("slider", { name: /text size/i });
    fireEvent.change(slider, { target: { value: "1.25" } });
    expect(onChange).toHaveBeenCalledWith({ fontScale: 1.25 });
  });

  it("auto-night is a switch that reports the change", () => {
    const { onChange } = renderControls({ autoNight: false });
    fireEvent.click(screen.getByRole("switch", { name: /night/i }));
    expect(onChange).toHaveBeenCalledWith({ autoNight: true });
  });

  it("reading mode toggles between scroll and paged", () => {
    const { onChange } = renderControls({ readingMode: "scroll" });
    fireEvent.click(screen.getByRole("radio", { name: /paged/i }));
    expect(onChange).toHaveBeenCalledWith({ readingMode: "paged" });
  });

  it("exposes brightness and read-aloud rate sliders", () => {
    renderControls();
    expect(screen.getByRole("slider", { name: /brightness/i })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: /read.?aloud.*speed|speed/i })).toBeInTheDocument();
  });

  it("offers a voice picker with a system-default option", () => {
    renderControls();
    const select = screen.getByRole("combobox", { name: /voice/i });
    expect(select).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /system default/i })).toBeInTheDocument();
  });
});
