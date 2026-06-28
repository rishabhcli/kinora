import { describe, it, expect } from "vitest";
import {
  passwordRequirements,
  meetsPolicy,
  isCommonPassword,
  hasObviousSequence,
  assessPassword,
  validateNewPassword,
  validatePasswordChange,
  MIN_LENGTH,
} from "./password";

describe("passwordRequirements", () => {
  it("flags each class", () => {
    const reqs = passwordRequirements("Abcdef1!");
    const met = Object.fromEntries(reqs.map((r) => [r.id, r.met]));
    expect(met).toEqual({ length: true, lower: true, upper: true, digit: true, symbol: true });
  });
});

describe("meetsPolicy", () => {
  it("requires min length + 3 classes", () => {
    expect(meetsPolicy("short1A")).toBe(false); // 7 chars
    expect(meetsPolicy("abcdefgh")).toBe(false); // 1 class
    expect(meetsPolicy("abcdEF12")).toBe(true); // 3 classes, 8 chars
  });
});

describe("isCommonPassword", () => {
  it("catches known weak ones + base+digits", () => {
    expect(isCommonPassword("password")).toBe(true);
    expect(isCommonPassword("Password123")).toBe(true); // base "password"
    expect(isCommonPassword("kinora")).toBe(true);
    expect(isCommonPassword("Tr0ub4dor&3")).toBe(false);
  });
});

describe("hasObviousSequence", () => {
  it("detects runs, repeats, and keyboard walks", () => {
    expect(hasObviousSequence("abcde")).toBe(true);
    expect(hasObviousSequence("4321x")).toBe(true);
    expect(hasObviousSequence("aaaa")).toBe(true);
    expect(hasObviousSequence("qwert")).toBe(true);
    expect(hasObviousSequence("x9!qP2")).toBe(false);
  });
});

describe("assessPassword", () => {
  it("scores empty as 0 with no label", () => {
    const a = assessPassword("");
    expect(a.score).toBe(0);
    expect(a.label).toBe("");
    expect(a.meetsPolicy).toBe(false);
  });
  it("knocks down common passwords and warns", () => {
    const a = assessPassword("password");
    expect(a.score).toBe(1);
    expect(a.warning).toMatch(/common/i);
  });
  it("knocks down sequences", () => {
    const a = assessPassword("abcdefgh");
    expect(a.score).toBeLessThanOrEqual(2);
    expect(a.warning).toBeTruthy();
  });
  it("rates a strong password highly with no warning", () => {
    const a = assessPassword("Tr0ub4dor&3xKq");
    expect(a.score).toBeGreaterThanOrEqual(3);
    expect(a.meetsPolicy).toBe(true);
    expect(a.warning).toBeUndefined();
  });
  it("warns about short length", () => {
    expect(assessPassword("Ab1!").warning).toContain(String(MIN_LENGTH));
  });
});

describe("validateNewPassword", () => {
  it("requires policy, non-common, matching confirm", () => {
    expect(validateNewPassword("", "")).toMatch(/enter/i);
    expect(validateNewPassword("short", "short")).toMatch(/least/i);
    // passes the variety policy but is a common base → flagged as common
    expect(validateNewPassword("Password123", "Password123")).toMatch(/common/i);
    expect(validateNewPassword("Str0ng&Pass", "nope")).toMatch(/match/i);
    expect(validateNewPassword("Str0ng&Pass", "Str0ng&Pass")).toBeNull();
  });
});

describe("validatePasswordChange", () => {
  it("requires current, a real change, and a valid new pw", () => {
    expect(validatePasswordChange("", "x", "x")).toMatch(/current/i);
    expect(validatePasswordChange("same", "same", "same")).toMatch(/haven't used/i);
    expect(validatePasswordChange("old", "Str0ng&Pass", "Str0ng&Pass")).toBeNull();
  });
});
