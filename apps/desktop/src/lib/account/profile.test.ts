import { describe, it, expect } from "vitest";
import {
  emptyProfile,
  parseProfile,
  displayNameOf,
  initialsOf,
  avatarColor,
  validateProfile,
  isProfileValid,
  normalizeHandle,
  bioRemaining,
  PROFILE_LIMITS,
} from "./profile";

describe("emptyProfile / parseProfile", () => {
  it("emptyProfile carries id+email", () => {
    expect(emptyProfile("u1", "a@x.com")).toEqual({ id: "u1", email: "a@x.com", displayName: "" });
  });
  it("requires id and email", () => {
    expect(parseProfile({ id: "u1" })).toBeNull();
    expect(parseProfile({ email: "a@x.com" })).toBeNull();
  });
  it("maps snake_case + name → displayName", () => {
    const p = parseProfile({ id: "u1", email: "a@x.com", display_name: "Ada", avatar_url: "/a.png" })!;
    expect(p).toMatchObject({ displayName: "Ada", avatarUrl: "/a.png" });
    const p2 = parseProfile({ id: "u1", email: "a@x.com", name: "Grace" })!;
    expect(p2.displayName).toBe("Grace");
  });
});

describe("displayNameOf", () => {
  it("prefers display name, falls back to local-part", () => {
    expect(displayNameOf({ displayName: "Ada", email: "x@y.com" })).toBe("Ada");
    expect(displayNameOf({ displayName: "  ", email: "ada@y.com" })).toBe("ada");
  });
});

describe("initialsOf", () => {
  it("derives 1–2 initials", () => {
    expect(initialsOf({ displayName: "Ada Lovelace", email: "" })).toBe("AL");
    expect(initialsOf({ displayName: "Cher", email: "" })).toBe("CH");
    expect(initialsOf({ displayName: "", email: "ada@y.com" })).toBe("AD");
    expect(initialsOf({ displayName: "Ada B Lovelace", email: "" })).toBe("AL");
  });
});

describe("avatarColor", () => {
  it("is deterministic and complete", () => {
    const a = avatarColor("u1");
    expect(avatarColor("u1")).toEqual(a);
    expect(a.gradient).toContain("linear-gradient");
    expect(a.gradient).toContain(a.from);
    expect(a.gradient).toContain(a.to);
  });
  it("differs across seeds (probabilistically distinct)", () => {
    const seeds = ["a", "b", "c", "d", "e", "f", "g", "h"];
    const grads = new Set(seeds.map((s) => avatarColor(s).gradient));
    expect(grads.size).toBeGreaterThan(1);
  });
});

describe("validateProfile", () => {
  it("accepts a clean profile", () => {
    expect(isProfileValid(validateProfile({ displayName: "Ada", handle: "ada_l", bio: "hi" }))).toBe(true);
  });
  it("rejects over-long names, bad handles, long bios", () => {
    const long = "x".repeat(PROFILE_LIMITS.displayName + 1);
    expect(validateProfile({ displayName: long }).displayName).toBeTruthy();
    expect(validateProfile({ handle: "ab" }).handle).toBeTruthy();
    expect(validateProfile({ handle: "bad handle!" }).handle).toBeTruthy();
    expect(validateProfile({ bio: "x".repeat(PROFILE_LIMITS.bio + 1) }).bio).toBeTruthy();
  });
  it("allows an empty handle", () => {
    expect(validateProfile({ handle: "" }).handle).toBeUndefined();
  });
});

describe("normalizeHandle / bioRemaining", () => {
  it("strips @ and lowercases", () => {
    expect(normalizeHandle("  @AdaL ")).toBe("adal");
  });
  it("counts remaining bio chars", () => {
    expect(bioRemaining("hello")).toBe(PROFILE_LIMITS.bio - 5);
  });
});
