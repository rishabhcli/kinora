import { describe, it, expect } from "vitest";
import {
  toBase32,
  isValidSecret,
  formatSecret,
  otpAuthUri,
  normalizeCode,
  isValidCodeShape,
  generateRecoveryCodes,
  normalizeRecoveryCode,
  isValidRecoveryCodeShape,
  recoveryCodesText,
  enrollReducer,
  enrollProgress,
  initialEnrollState,
} from "./mfa";
import type { RandomBytes } from "./store";

// A deterministic byte source: a fixed repeating pattern.
const fixedRand: RandomBytes = (n) => Uint8Array.from({ length: n }, (_, i) => (i * 7) % 256);

describe("base32", () => {
  it("encodes known vectors", () => {
    // "f" = 0x66 → "MY" in RFC 4648 base32 (unpadded)
    expect(toBase32(new Uint8Array([0x66]))).toBe("MY");
    expect(toBase32(new Uint8Array([0x66, 0x6f]))).toBe("MZXQ");
  });
});

describe("secret validation + formatting", () => {
  it("accepts long base32, rejects short/garbage", () => {
    expect(isValidSecret("JBSWY3DPEHPK3PXP")).toBe(true);
    expect(isValidSecret("jbswy3dpehpk3pxp")).toBe(true); // case-insensitive
    expect(isValidSecret("short")).toBe(false);
    expect(isValidSecret("0189!!!!!!!!!!!!")).toBe(false); // 0/1/! not in alphabet
  });
  it("groups into 4-char chunks", () => {
    expect(formatSecret("jbswy3dpehpk3pxp")).toBe("JBSW Y3DP EHPK 3PXP");
  });
});

describe("otpAuthUri", () => {
  it("builds a Key-Uri-Format string", () => {
    const uri = otpAuthUri({ account: "ada@x.com", secret: "JBSWY3DPEHPK3PXP", issuer: "Kinora" });
    expect(uri.startsWith("otpauth://totp/Kinora%3Aada%40x.com?")).toBe(true);
    expect(uri).toContain("secret=JBSWY3DPEHPK3PXP");
    expect(uri).toContain("issuer=Kinora");
    expect(uri).toContain("digits=6");
    expect(uri).toContain("period=30");
    expect(uri).toContain("algorithm=SHA1");
  });
});

describe("code shape", () => {
  it("normalizes and validates", () => {
    expect(normalizeCode("123 456")).toBe("123456");
    expect(isValidCodeShape("123 456")).toBe(true);
    expect(isValidCodeShape("12345")).toBe(false);
    expect(isValidCodeShape("12345678", 8)).toBe(true);
    expect(isValidCodeShape("12ab56")).toBe(false);
  });
});

describe("recovery codes", () => {
  it("generates the requested count in XXXX-XXXX shape, deterministic", () => {
    const set = generateRecoveryCodes(10, fixedRand, 999);
    expect(set.codes).toHaveLength(10);
    expect(set.generatedAt).toBe(999);
    for (const c of set.codes) expect(isValidRecoveryCodeShape(c)).toBe(true);
    // determinism
    expect(generateRecoveryCodes(3, fixedRand, 1).codes).toEqual(
      generateRecoveryCodes(3, fixedRand, 1).codes,
    );
  });
  it("normalizes and validates a typed code", () => {
    expect(normalizeRecoveryCode("ab cd-ef gh")).toBe("ABCD-EFGH");
    expect(isValidRecoveryCodeShape("abcd-efgh")).toBe(true);
    expect(isValidRecoveryCodeShape("abc-defg")).toBe(false);
  });
  it("renders a copy/download blob", () => {
    const text = recoveryCodesText({ codes: ["ABCD-1234", "WXYZ-5678"], generatedAt: 0 });
    expect(text).toContain("ABCD-1234");
    expect(text).toContain("WXYZ-5678");
    expect(text).toContain("recovery codes");
  });
});

describe("enrollment reducer (TOTP path)", () => {
  it("walks idle→method→scan→verify→recovery→done", () => {
    let s = enrollReducer(initialEnrollState, { type: "start" });
    expect(s.step).toBe("method");
    s = enrollReducer(s, { type: "choose", method: "totp" });
    expect(s).toMatchObject({ method: "totp", step: "scan" });
    s = enrollReducer(s, { type: "secretShown" });
    expect(s.step).toBe("verify");
    s = enrollReducer(s, { type: "verified" });
    expect(s).toMatchObject({ verified: true, step: "recovery" });
    s = enrollReducer(s, { type: "recoverySaved" });
    expect(s).toMatchObject({ recoverySaved: true, step: "done" });
  });

  it("passkey choice jumps straight to recovery", () => {
    let s = enrollReducer(initialEnrollState, { type: "start" });
    s = enrollReducer(s, { type: "choose", method: "passkey" });
    expect(s).toMatchObject({ method: "passkey", step: "recovery" });
  });

  it("ignores out-of-order events", () => {
    const s = enrollReducer({ ...initialEnrollState, step: "method" }, { type: "verified" });
    expect(s.step).toBe("method");
  });

  it("back steps and cancel resets", () => {
    const verify = { ...initialEnrollState, method: "totp" as const, step: "verify" as const };
    expect(enrollReducer(verify, { type: "back" }).step).toBe("scan");
    expect(enrollReducer(verify, { type: "cancel" })).toEqual(initialEnrollState);
    expect(enrollReducer({ ...initialEnrollState, step: "method" }, { type: "back" }).step).toBe("method");
  });
});

describe("enrollProgress", () => {
  it("is 0 at method and 1 at done for TOTP", () => {
    expect(enrollProgress({ ...initialEnrollState, method: "totp", step: "method" })).toBe(0);
    expect(enrollProgress({ ...initialEnrollState, method: "totp", step: "done" })).toBe(1);
    expect(enrollProgress({ ...initialEnrollState, method: "totp", step: "verify" })).toBeCloseTo(0.5);
  });
  it("uses the shorter passkey track", () => {
    expect(enrollProgress({ ...initialEnrollState, method: "passkey", step: "recovery" })).toBe(0.5);
  });
});
