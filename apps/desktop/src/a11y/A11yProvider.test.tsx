import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { A11yProvider } from "./A11yProvider";
import { clearAllShortcuts, registerShortcut } from "./keyboard";
import {
  setHighContrastOverride,
  setReducedTransparencyOverride,
} from "./displayPrefs";
import { setReducedMotionOverride } from "./useReducedMotionPref";

beforeEach(() => {
  localStorage.clear();
  clearAllShortcuts();
  setReducedMotionOverride(null);
  setHighContrastOverride(null);
  setReducedTransparencyOverride(null);
  document.documentElement.className = "";
});

afterEach(() => {
  clearAllShortcuts();
  document.documentElement.className = "";
});

describe("A11yProvider", () => {
  it("renders its children", () => {
    render(
      <A11yProvider>
        <button>app content</button>
      </A11yProvider>,
    );
    expect(screen.getByText("app content")).toBeInTheDocument();
  });

  it("renders a skip-to-content link as the first focusable element", () => {
    render(
      <A11yProvider>
        <main>body</main>
      </A11yProvider>,
    );
    expect(screen.getByRole("link", { name: /skip to content/i })).toBeInTheDocument();
  });

  it("reflects the high-contrast preference onto <html>", () => {
    render(
      <A11yProvider>
        <div>x</div>
      </A11yProvider>,
    );
    expect(document.documentElement).not.toHaveClass("kinora-high-contrast");
    act(() => setHighContrastOverride(true));
    expect(document.documentElement).toHaveClass("kinora-high-contrast");
  });

  it("reflects the reduced-transparency preference onto <html>", () => {
    render(
      <A11yProvider>
        <div>x</div>
      </A11yProvider>,
    );
    act(() => setReducedTransparencyOverride(true));
    expect(document.documentElement).toHaveClass("kinora-reduce-transparency");
  });

  it("opens the shortcut cheat-sheet on '?' and closes on Escape", () => {
    render(
      <A11yProvider>
        <div>x</div>
      </A11yProvider>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "?" });
    const dialog = screen.getByRole("dialog", { name: /keyboard shortcuts/i });
    expect(dialog).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("lists registered shortcuts (combo + description) in the cheat-sheet", () => {
    registerShortcut("mod+,", () => {}, { description: "Open settings", scope: "Global" });
    render(
      <A11yProvider>
        <div>x</div>
      </A11yProvider>,
    );
    fireEvent.keyDown(document.body, { key: "?" });
    expect(screen.getByText("Open settings")).toBeInTheDocument();
  });
});
