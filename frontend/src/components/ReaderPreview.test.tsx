import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ReaderPreview from "./ReaderPreview";

function mockMatchMedia(matches: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

function currentWord(): number {
  const node = screen.getByText(/word \d+\/\d+/);
  const match = node.textContent?.match(/word (\d+)\//);
  return match ? Number(match[1]) : -1;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  // matchMedia is assigned, not mocked, so clear it between tests.
  Reflect.deleteProperty(window, "matchMedia");
});

describe("ReaderPreview", () => {
  it("starts paused on the first word with a play control", () => {
    render(<ReaderPreview />);
    expect(screen.getByRole("button", { name: /play preview/i })).toBeTruthy();
    expect(currentWord()).toBe(1);
  });

  it("advances the focus word while playing, then can be paused", () => {
    vi.useFakeTimers();
    render(<ReaderPreview />);

    fireEvent.click(screen.getByRole("button", { name: /play preview/i }));
    expect(screen.getByRole("button", { name: /pause preview/i })).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(currentWord()).toBeGreaterThan(1);

    fireEvent.click(screen.getByRole("button", { name: /pause preview/i }));
    expect(screen.getByRole("button", { name: /play preview/i })).toBeTruthy();
  });

  it("steps word by word with the arrow keys", () => {
    render(<ReaderPreview />);
    const region = screen.getByRole("group", { name: /reading preview/i });

    fireEvent.keyDown(region, { key: "ArrowRight" });
    expect(currentWord()).toBe(2);
    fireEvent.keyDown(region, { key: "ArrowRight" });
    expect(currentWord()).toBe(3);
    fireEvent.keyDown(region, { key: "ArrowLeft" });
    expect(currentWord()).toBe(2);
    fireEvent.keyDown(region, { key: "Home" });
    expect(currentWord()).toBe(1);
  });

  it("restarts back to the first word", () => {
    render(<ReaderPreview />);
    const region = screen.getByRole("group", { name: /reading preview/i });
    fireEvent.keyDown(region, { key: "ArrowRight" });
    fireEvent.keyDown(region, { key: "ArrowRight" });
    expect(currentWord()).toBe(3);
    fireEvent.click(screen.getByRole("button", { name: /restart/i }));
    expect(currentWord()).toBe(1);
  });

  it("renders the Ken Burns motion when motion is allowed", () => {
    mockMatchMedia(false);
    const { container } = render(<ReaderPreview />);
    expect(container.innerHTML.includes("ken-burns")).toBe(true);
  });

  it("drops the Ken Burns motion when the user prefers reduced motion", () => {
    mockMatchMedia(true);
    const { container } = render(<ReaderPreview />);
    expect(container.innerHTML.includes("ken-burns")).toBe(false);
  });
});
