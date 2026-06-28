import { describe, it, expect, afterEach } from "vitest";
import { ensureDiscoveryStyles, resetDiscoveryStylesForTest } from "./styleInjection";

afterEach(() => resetDiscoveryStylesForTest());

describe("ensureDiscoveryStyles", () => {
  it("injects a single <style> element with the discovery keyframes", () => {
    ensureDiscoveryStyles();
    const el = document.getElementById("kinora-discovery-styles");
    expect(el).not.toBeNull();
    expect(el!.textContent).toContain("@keyframes discovery-pop");
    expect(el!.textContent).toContain("discovery-row-in");
  });

  it("is idempotent (no duplicate style tags)", () => {
    ensureDiscoveryStyles();
    ensureDiscoveryStyles();
    ensureDiscoveryStyles();
    expect(document.querySelectorAll("#kinora-discovery-styles").length).toBe(1);
  });

  it("reset removes the injected element", () => {
    ensureDiscoveryStyles();
    resetDiscoveryStylesForTest();
    expect(document.getElementById("kinora-discovery-styles")).toBeNull();
  });
});
