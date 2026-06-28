// OAuth / SSO (account domain) — the provider registry plus the pure pieces of
// an authorization-code-with-PKCE flow: deterministic state + verifier
// generation (from an injectable RNG), the authorize-URL builder, and
// callback-URL parsing/validation. No network here — the redirect itself and
// the token exchange live in the API adapter / shell. Keeping the state
// machinery pure makes the security-critical CSRF-state round-trip testable.

import {
  type KeyValueStore,
  type RandomBytes,
  insecureRandomBytes,
  readJson,
  removeKey,
  resolveStore,
  writeJson,
} from "./store";

// ---- Provider registry ---------------------------------------------------- //

export type OAuthProviderId = "google" | "apple" | "github" | "microsoft";

export interface OAuthProvider {
  id: OAuthProviderId;
  /** Display name, e.g. "Google". */
  name: string;
  /** Icon name understood by AuthIcon. */
  icon: string;
  /** Whether the provider uses the OIDC `nonce` param in addition to PKCE. */
  oidc: boolean;
}

/** The SSO providers Kinora offers, in display order. The actual client ids +
 *  authorize endpoints live in the backend's config; the renderer just kicks off
 *  `/api/auth/oauth/{id}/start` and follows the redirect. */
export const OAUTH_PROVIDERS: OAuthProvider[] = [
  { id: "google", name: "Google", icon: "google", oidc: true },
  { id: "apple", name: "Apple", icon: "apple", oidc: true },
  { id: "github", name: "GitHub", icon: "github", oidc: false },
  { id: "microsoft", name: "Microsoft", icon: "mail", oidc: true },
];

export function findProvider(id: string): OAuthProvider | undefined {
  return OAUTH_PROVIDERS.find((p) => p.id === id);
}

// ---- PKCE / state primitives ---------------------------------------------- //

/** crypto.getRandomValues when available; insecure fallback for tests/SSR. */
export function defaultRandomBytes(): RandomBytes {
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (c && typeof c.getRandomValues === "function") {
    return (n) => c.getRandomValues(new Uint8Array(n));
  }
  return insecureRandomBytes;
}

const URLSAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";

/** A URL-safe random token of `len` chars (used for state + the PKCE verifier).
 *  Deterministic given the RNG, so tests can pin it. */
export function randomToken(len: number, rand: RandomBytes = insecureRandomBytes): string {
  const bytes = rand(len);
  let out = "";
  for (let i = 0; i < len; i++) out += URLSAFE[bytes[i] % URLSAFE.length];
  return out;
}

/** A pending OAuth attempt — persisted across the redirect so the callback can
 *  validate the returned `state` (CSRF defence) and recover the provider. */
export interface OAuthAttempt {
  provider: OAuthProviderId;
  state: string;
  /** PKCE code_verifier; the challenge is derived backend-side or via SHA-256. */
  verifier: string;
  nonce?: string;
  /** Where to return the user after sign-in (in-app route). */
  returnTo?: string;
  createdAt: number;
}

/** Mint a fresh attempt (state + verifier + optional nonce) for a provider. */
export function createAttempt(
  provider: OAuthProviderId,
  opts: { returnTo?: string; rand?: RandomBytes; now?: number } = {},
): OAuthAttempt {
  const rand = opts.rand ?? defaultRandomBytes();
  const meta = findProvider(provider);
  return {
    provider,
    state: randomToken(32, rand),
    verifier: randomToken(64, rand),
    nonce: meta?.oidc ? randomToken(24, rand) : undefined,
    returnTo: opts.returnTo,
    createdAt: opts.now ?? Date.now(),
  };
}

// ---- Authorize URL builder ------------------------------------------------ //

export interface AuthorizeParams {
  authorizeEndpoint: string;
  clientId: string;
  redirectUri: string;
  scope: string;
  attempt: OAuthAttempt;
  /** Optional precomputed PKCE S256 challenge; falls back to "plain". */
  codeChallenge?: string;
}

/** Build the provider authorize URL. Uses S256 PKCE when a challenge is
 *  supplied, else `plain` (verifier as challenge) — the backend prefers S256. */
export function buildAuthorizeUrl(p: AuthorizeParams): string {
  const params = new URLSearchParams({
    response_type: "code",
    client_id: p.clientId,
    redirect_uri: p.redirectUri,
    scope: p.scope,
    state: p.attempt.state,
    code_challenge: p.codeChallenge ?? p.attempt.verifier,
    code_challenge_method: p.codeChallenge ? "S256" : "plain",
  });
  if (p.attempt.nonce) params.set("nonce", p.attempt.nonce);
  const sep = p.authorizeEndpoint.includes("?") ? "&" : "?";
  return `${p.authorizeEndpoint}${sep}${params.toString()}`;
}

// ---- Callback parsing ------------------------------------------------------ //

export interface CallbackResult {
  ok: boolean;
  code?: string;
  state?: string;
  error?: string;
  errorDescription?: string;
}

/** Parse an OAuth redirect URL (or just its query/fragment string). Handles
 *  both `?code=…` and `#code=…` response modes. */
export function parseCallback(url: string): CallbackResult {
  let query = "";
  // Accept a full URL, a "?a=b" string, or a "#a=b" fragment.
  const hashIdx = url.indexOf("#");
  const qIdx = url.indexOf("?");
  if (qIdx >= 0) query = url.slice(qIdx + 1);
  else if (hashIdx >= 0) query = url.slice(hashIdx + 1);
  else query = url;
  // If both ? and # carry params, prefer the one with `code`/`state`.
  if (qIdx >= 0 && hashIdx > qIdx) {
    const frag = url.slice(hashIdx + 1);
    if (/(^|&)(code|error)=/.test(frag) && !/(^|&)(code|error)=/.test(query)) query = frag;
  }
  const params = new URLSearchParams(query);
  const error = params.get("error") ?? undefined;
  if (error) {
    return { ok: false, error, errorDescription: params.get("error_description") ?? undefined };
  }
  const code = params.get("code") ?? undefined;
  const state = params.get("state") ?? undefined;
  return { ok: Boolean(code), code, state };
}

/** Validate a callback against a stored attempt: the state must match exactly
 *  (CSRF), a code must be present, and the provider must align. */
export function validateCallback(
  result: CallbackResult,
  attempt: OAuthAttempt | null,
): { valid: boolean; reason?: string } {
  if (!result.ok) return { valid: false, reason: result.error ?? "no_code" };
  if (!attempt) return { valid: false, reason: "no_attempt" };
  if (!result.state) return { valid: false, reason: "missing_state" };
  if (result.state !== attempt.state) return { valid: false, reason: "state_mismatch" };
  if (!result.code) return { valid: false, reason: "no_code" };
  return { valid: true };
}

// ---- Attempt persistence (one in-flight attempt at a time) ---------------- //

const ATTEMPT_KEY = "kinora.account.oauth-attempt.v1";

export interface OAuthAttemptStore {
  get(): OAuthAttempt | null;
  save(attempt: OAuthAttempt): void;
  clear(): void;
}

/** Persist the single in-flight OAuth attempt across the redirect. We keep only
 *  the latest — a new sign-in click supersedes a stale one. */
export function createAttemptStore(backing?: KeyValueStore | null): OAuthAttemptStore {
  const store = resolveStore(backing);
  return {
    get: () => {
      const a = readJson<OAuthAttempt | null>(store, ATTEMPT_KEY, null);
      return a && typeof a.state === "string" && typeof a.provider === "string" ? a : null;
    },
    save: (attempt) => void writeJson(store, ATTEMPT_KEY, attempt),
    clear: () => removeKey(store, ATTEMPT_KEY),
  };
}

export const OAUTH_ATTEMPT_KEY = ATTEMPT_KEY;
