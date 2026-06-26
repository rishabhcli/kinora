import { describe, it, expect, beforeEach } from "vitest";
import { announce } from "./announce";

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("announce", () => {
  it("creates a polite status region and writes the message", () => {
    announce("Generating scene 3");
    const region = document.querySelector('[aria-live="polite"]')!;
    expect(region).toBeInTheDocument();
    expect(region.getAttribute("role")).toBe("status");
    expect(region.textContent).toContain("Generating scene 3");
  });

  it("uses an assertive alert region when asked", () => {
    announce("Render failed", "assertive");
    const region = document.querySelector('[aria-live="assertive"]')!;
    expect(region.getAttribute("role")).toBe("alert");
    expect(region.textContent).toContain("Render failed");
  });

  it("reuses one region per politeness (no duplicates)", () => {
    announce("a");
    announce("b");
    expect(document.querySelectorAll('[aria-live="polite"]')).toHaveLength(1);
  });

  it("re-announces an identical consecutive message (content changes)", () => {
    announce("Saved");
    const region = document.querySelector('[aria-live="polite"]')!;
    const first = region.textContent;
    announce("Saved");
    expect(region.textContent).not.toBe(first);
    expect(region.textContent).toContain("Saved");
  });

  it("renders the region visually hidden", () => {
    announce("x");
    const region = document.querySelector('[aria-live="polite"]') as HTMLElement;
    expect(region.style.position).toBe("absolute");
    expect(region.style.width).toBe("1px");
  });
});
