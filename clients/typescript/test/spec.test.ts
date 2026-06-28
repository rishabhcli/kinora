import { describe, it, expect } from "vitest";
import { ENDPOINTS, EVENTS, ERROR_TYPES, API_PREFIX, endpointsByTag, fullPath } from "../src/spec.js";

describe("spec integrity", () => {
  it("has the expected endpoint count and unique operation ids", () => {
    expect(ENDPOINTS.length).toBeGreaterThanOrEqual(30);
    const ids = ENDPOINTS.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("every path is relative (no leading /api)", () => {
    for (const e of ENDPOINTS) {
      expect(e.path.startsWith("/api")).toBe(false);
      expect(e.path.startsWith("/")).toBe(true);
      expect(fullPath(e)).toBe(`${API_PREFIX}${e.path}`);
    }
  });

  it("groups endpoints by tag", () => {
    const groups = endpointsByTag();
    expect(groups.has("auth")).toBe(true);
    expect(groups.has("sessions")).toBe(true);
    expect(groups.get("auth")!.some((e) => e.id === "login")).toBe(true);
  });

  it("documents the core events and error types", () => {
    const names = EVENTS.map((e) => e.name);
    expect(names).toContain("clip_ready");
    expect(names).toContain("buffer_state");
    expect(names).toContain("conflict_choice");
    const errTypes = ERROR_TYPES.map((e) => e.type);
    expect(errTypes).toContain("book_not_found");
    expect(errTypes).toContain("budget_exceeded");
  });

  it("auth endpoints do not require a token; others do", () => {
    const login = ENDPOINTS.find((e) => e.id === "login")!;
    expect(login.auth).toBe(false);
    const me = ENDPOINTS.find((e) => e.id === "me")!;
    expect(me.auth).toBe(true);
  });
});
