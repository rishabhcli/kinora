import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock the shared seam (lib/api.ts) so the adapter is tested in isolation. We
// keep the real ApiError so the soft-call status checks work.
const httpMock = vi.fn();
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, http: (...args: unknown[]) => httpMock(...args) };
});

import { ApiError } from "../api";
import {
  getProfile,
  updateProfile,
  getPreferences,
  changePassword,
  deleteAccount,
  beginTotpEnrollment,
  confirmTotpEnrollment,
  listSecurityEvents,
} from "./account";
import { isValidSecret, isValidRecoveryCodeShape } from "../account";
import type { Profile } from "../account";

beforeEach(() => httpMock.mockReset());

describe("getProfile", () => {
  it("returns the enriched profile when /account/profile responds", async () => {
    httpMock.mockResolvedValueOnce({ id: "u1", email: "a@x.com", display_name: "Ada" });
    const p = await getProfile();
    expect(p).toMatchObject({ id: "u1", displayName: "Ada" });
  });

  it("falls back to /auth/me when /account/profile 404s", async () => {
    httpMock
      .mockRejectedValueOnce(new ApiError(404, "nope"))
      .mockResolvedValueOnce({ id: "u1", email: "a@x.com" });
    const p = await getProfile();
    expect(p).toMatchObject({ id: "u1", email: "a@x.com" });
    expect(httpMock).toHaveBeenCalledWith("/api/auth/me");
  });

  it("returns null when both endpoints fail softly", async () => {
    httpMock
      .mockRejectedValueOnce(new ApiError(404, "nope")) // /account/profile
      .mockRejectedValueOnce(new ApiError(404, "nope")); // /auth/me
    expect(await getProfile()).toBeNull();
  });
});

describe("updateProfile", () => {
  const current: Profile = { id: "u1", email: "a@x.com", displayName: "Ada" };

  it("PATCHes snake_case and returns the saved profile", async () => {
    httpMock.mockResolvedValueOnce({ id: "u1", email: "a@x.com", display_name: "Grace" });
    const saved = await updateProfile(current, { displayName: "Grace" });
    expect(saved.displayName).toBe("Grace");
    const [, init] = httpMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ display_name: "Grace" });
  });

  it("returns the optimistic merge when the endpoint is unavailable", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(501, "soon"));
    const saved = await updateProfile(current, { bio: "hello" });
    expect(saved).toMatchObject({ displayName: "Ada", bio: "hello" });
  });
});

describe("getPreferences", () => {
  it("merges backend prefs through the validator", async () => {
    httpMock.mockResolvedValueOnce({ email: { digest: "monthly", junk: 1 } });
    const p = await getPreferences();
    expect(p?.email.digest).toBe("monthly");
    expect(p?.email.security).toBe(true);
  });
  it("returns null when unavailable", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect(await getPreferences()).toBeNull();
  });
});

describe("changePassword", () => {
  it("resolves true on success", async () => {
    httpMock.mockResolvedValueOnce(null);
    expect(await changePassword({ current_password: "a", new_password: "b" })).toBe(true);
  });
  it("rethrows a 401 (wrong current password)", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(401, "wrong"));
    await expect(changePassword({ current_password: "a", new_password: "b" })).rejects.toBeInstanceOf(ApiError);
  });
  it("treats a missing endpoint as success (demo)", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect(await changePassword({ current_password: "a", new_password: "b" })).toBe(true);
  });
});

describe("deleteAccount", () => {
  it("uses the backend's deleted_at, else a 30-day default", async () => {
    httpMock.mockResolvedValueOnce({ deleted_at: "2025-01-01T00:00:00Z" });
    expect((await deleteAccount("a@x.com")).deletedAt).toBe(Date.parse("2025-01-01T00:00:00Z"));

    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    const { deletedAt } = await deleteAccount("a@x.com");
    expect(deletedAt).toBeGreaterThan(Date.now());
  });
});

describe("MFA enrollment", () => {
  it("begins with a backend secret when present", async () => {
    httpMock.mockResolvedValueOnce({ secret: "JBSWY3DPEHPK3PXPABCDEF", otpauth_uri: "otpauth://x" });
    const e = await beginTotpEnrollment("a@x.com");
    expect(e.secret).toBe("JBSWY3DPEHPK3PXPABCDEF");
    expect(e.otpauthUri).toBe("otpauth://x");
  });

  it("synthesises a valid demo secret + otpauth URI when unavailable", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    const e = await beginTotpEnrollment("a@x.com");
    expect(isValidSecret(e.secret)).toBe(true);
    expect(e.otpauthUri.startsWith("otpauth://totp/")).toBe(true);
  });

  it("confirm returns recovery codes (backend or demo) in valid shape", async () => {
    httpMock.mockResolvedValueOnce({ verified: true, recovery_codes: ["ABCD-1234", "WXYZ-5678"] });
    const r1 = await confirmTotpEnrollment("123456");
    expect(r1.verified).toBe(true);
    expect(r1.recovery.codes).toEqual(["ABCD-1234", "WXYZ-5678"]);

    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    const r2 = await confirmTotpEnrollment("123456");
    expect(r2.verified).toBe(true);
    expect(r2.recovery.codes.every(isValidRecoveryCodeShape)).toBe(true);
  });
});

describe("listSecurityEvents", () => {
  it("parses + sorts events, [] when unavailable", async () => {
    httpMock.mockResolvedValueOnce([
      { id: "a", kind: "login", at: 1 },
      { id: "b", kind: "password_changed", at: 9 },
    ]);
    expect((await listSecurityEvents()).map((e) => e.id)).toEqual(["b", "a"]);

    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect(await listSecurityEvents()).toEqual([]);
  });
});
