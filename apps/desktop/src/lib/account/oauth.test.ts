import { describe, it, expect } from "vitest";
import { memoryStore } from "./store";
import {
  OAUTH_PROVIDERS,
  findProvider,
  randomToken,
  createAttempt,
  buildAuthorizeUrl,
  parseCallback,
  validateCallback,
  createAttemptStore,
} from "./oauth";
import type { RandomBytes } from "./store";

const fixedRand: RandomBytes = (n) => Uint8Array.from({ length: n }, (_, i) => (i * 13 + 1) % 256);

describe("provider registry", () => {
  it("has the four SSO providers and finds by id", () => {
    expect(OAUTH_PROVIDERS.map((p) => p.id).sort()).toEqual(["apple", "github", "google", "microsoft"]);
    expect(findProvider("google")?.oidc).toBe(true);
    expect(findProvider("github")?.oidc).toBe(false);
    expect(findProvider("nope")).toBeUndefined();
  });
});

describe("randomToken", () => {
  it("is the requested length, url-safe, deterministic for a fixed RNG", () => {
    const a = randomToken(32, fixedRand);
    const b = randomToken(32, fixedRand);
    expect(a).toHaveLength(32);
    expect(a).toBe(b);
    expect(a).toMatch(/^[A-Za-z0-9\-._~]+$/);
  });
});

describe("createAttempt", () => {
  it("includes a nonce only for OIDC providers", () => {
    const g = createAttempt("google", { rand: fixedRand, now: 1, returnTo: "/account" });
    expect(g.nonce).toBeTruthy();
    expect(g.returnTo).toBe("/account");
    expect(g.state).toHaveLength(32);
    expect(g.verifier).toHaveLength(64);
    const gh = createAttempt("github", { rand: fixedRand, now: 1 });
    expect(gh.nonce).toBeUndefined();
  });
});

describe("buildAuthorizeUrl", () => {
  const attempt = createAttempt("google", { rand: fixedRand, now: 1 });
  it("uses S256 when a challenge is given, else plain", () => {
    const s256 = buildAuthorizeUrl({
      authorizeEndpoint: "https://accounts.example/o/auth",
      clientId: "cid",
      redirectUri: "https://app/cb",
      scope: "openid email",
      attempt,
      codeChallenge: "CHAL",
    });
    expect(s256).toContain("code_challenge=CHAL");
    expect(s256).toContain("code_challenge_method=S256");
    // state rides as a query param; parse it back rather than match the
    // form-urlencoded spelling (URLSearchParams encodes ~ → %7E).
    expect(new URL(s256).searchParams.get("state")).toBe(attempt.state);
    expect(s256).toContain("nonce=");

    const plain = buildAuthorizeUrl({
      authorizeEndpoint: "https://accounts.example/o/auth?x=1",
      clientId: "cid",
      redirectUri: "https://app/cb",
      scope: "openid",
      attempt,
    });
    expect(plain).toContain("code_challenge_method=plain");
    expect(plain).toContain("&"); // appended to existing query
  });
});

describe("parseCallback", () => {
  it("parses a full URL with query", () => {
    const r = parseCallback("https://app/cb?code=abc&state=xyz");
    expect(r).toMatchObject({ ok: true, code: "abc", state: "xyz" });
  });
  it("parses a fragment response", () => {
    const r = parseCallback("https://app/cb#code=abc&state=xyz");
    expect(r).toMatchObject({ ok: true, code: "abc" });
  });
  it("surfaces provider errors", () => {
    const r = parseCallback("https://app/cb?error=access_denied&error_description=nope");
    expect(r).toMatchObject({ ok: false, error: "access_denied", errorDescription: "nope" });
  });
  it("accepts a bare query string", () => {
    expect(parseCallback("code=z&state=s")).toMatchObject({ ok: true, code: "z", state: "s" });
  });
});

describe("validateCallback", () => {
  const attempt = createAttempt("google", { rand: fixedRand, now: 1 });
  it("passes when state matches and a code is present", () => {
    const r = parseCallback(`https://app/cb?code=abc&state=${attempt.state}`);
    expect(validateCallback(r, attempt)).toEqual({ valid: true });
  });
  it("fails on state mismatch (CSRF defence)", () => {
    const r = parseCallback("https://app/cb?code=abc&state=evil");
    expect(validateCallback(r, attempt)).toEqual({ valid: false, reason: "state_mismatch" });
  });
  it("fails with no attempt or no code", () => {
    expect(validateCallback({ ok: true, code: "c", state: "s" }, null).reason).toBe("no_attempt");
    expect(validateCallback({ ok: false, error: "x" }, attempt).reason).toBe("x");
    expect(validateCallback({ ok: true, code: "c" }, attempt).reason).toBe("missing_state");
  });
});

describe("createAttemptStore", () => {
  it("saves, gets, clears, and rejects malformed", () => {
    const backing = memoryStore();
    const store = createAttemptStore(backing);
    expect(store.get()).toBeNull();
    const a = createAttempt("apple", { rand: fixedRand, now: 1 });
    store.save(a);
    expect(store.get()?.state).toBe(a.state);
    expect(createAttemptStore(backing).get()?.provider).toBe("apple");
    store.clear();
    expect(store.get()).toBeNull();
  });
  it("ignores corrupt persisted attempts", () => {
    const backing = memoryStore({ "kinora.account.oauth-attempt.v1": '{"junk":1}' });
    expect(createAttemptStore(backing).get()).toBeNull();
  });
});
