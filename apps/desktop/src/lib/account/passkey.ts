// Passkeys / WebAuthn (account domain) — capability detection and the
// pure credential-registry model behind "Sign in with a passkey" and the
// security panel's passkey list. The actual navigator.credentials.create/get
// calls are isolated in `webauthnAvailable()`-gated helpers; everything the UI
// reasons about (which passkeys exist, labels, sorting, base64url codec) is pure
// and testable with no DOM.

import { type KeyValueStore, readJson, resolveStore, writeJson } from "./store";

// ---- Capability detection ------------------------------------------------- //

interface PublicKeyCredentialCtor {
  isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean>;
  isConditionalMediationAvailable?: () => Promise<boolean>;
}

/** True if this renderer exposes the WebAuthn API at all. Feature-detected so
 *  the passkey button hides cleanly where it can't work (older Electron, etc). */
export function webauthnAvailable(nav: Navigator | undefined = globalThisNavigator()): boolean {
  if (!nav) return false;
  const creds = (nav as Navigator & { credentials?: unknown }).credentials;
  const hasPkc = typeof (globalThis as Record<string, unknown>).PublicKeyCredential !== "undefined";
  return Boolean(creds) && hasPkc;
}

/** Whether a *platform* authenticator (Touch ID / Windows Hello) is present.
 *  Async because the spec query is async; resolves false when unsupported. */
export async function platformAuthenticatorAvailable(): Promise<boolean> {
  const Pkc = (globalThis as Record<string, unknown>).PublicKeyCredential as
    | PublicKeyCredentialCtor
    | undefined;
  if (!Pkc?.isUserVerifyingPlatformAuthenticatorAvailable) return false;
  try {
    return await Pkc.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch {
    return false;
  }
}

/** Whether conditional mediation ("passkey autofill") is supported — lets us
 *  decide whether to request it on the sign-in field. */
export async function conditionalMediationAvailable(): Promise<boolean> {
  const Pkc = (globalThis as Record<string, unknown>).PublicKeyCredential as
    | PublicKeyCredentialCtor
    | undefined;
  if (!Pkc?.isConditionalMediationAvailable) return false;
  try {
    return await Pkc.isConditionalMediationAvailable();
  } catch {
    return false;
  }
}

function globalThisNavigator(): Navigator | undefined {
  return (globalThis as { navigator?: Navigator }).navigator;
}

// ---- base64url codec (no DOM dependency beyond atob/btoa) ----------------- //

/** Encode bytes to base64url (RFC 4648 §5, no padding) — the wire format for
 *  WebAuthn challenge/credential ids. */
export function toBase64Url(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  const b64 = btoaSafe(bin);
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Decode base64url back to bytes. */
export function fromBase64Url(s: string): Uint8Array {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atobSafe(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function btoaSafe(s: string): string {
  const g = globalThis as { btoa?: (s: string) => string };
  if (g.btoa) return g.btoa(s);
  // Node fallback (tests): Buffer is global there.
  return Buffer.from(s, "binary").toString("base64");
}

function atobSafe(s: string): string {
  const g = globalThis as { atob?: (s: string) => string };
  if (g.atob) return g.atob(s);
  return Buffer.from(s, "base64").toString("binary");
}

// ---- Credential registry (model) ------------------------------------------ //

export type AuthenticatorKind = "platform" | "cross-platform" | "unknown";

/** A registered passkey, as the backend would store + return it. The renderer
 *  caches the list so the security panel paints offline-first. */
export interface PasskeyCredential {
  id: string; // credential id (base64url)
  /** A friendly name the user can rename, e.g. "MacBook Touch ID". */
  label: string;
  kind: AuthenticatorKind;
  createdAt: number; // epoch ms
  lastUsedAt?: number; // epoch ms
  /** True for the credential this device most likely owns (best-effort). */
  thisDevice?: boolean;
}

const CACHE_KEY = "kinora.account.passkeys.v1";

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.length ? v : undefined;
}

function asKind(v: unknown): AuthenticatorKind {
  return v === "platform" || v === "cross-platform" ? v : "unknown";
}

function asMs(v: unknown, fallback: number): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const t = Date.parse(v);
    if (!Number.isNaN(t)) return t;
  }
  return fallback;
}

/** Parse a raw passkey row, dropping nothing but an id. */
export function parsePasskey(row: unknown): PasskeyCredential | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Record<string, unknown>;
  const id = str(r.id);
  if (!id) return null;
  const createdAt = asMs(r.createdAt ?? r.created_at, Date.now());
  return {
    id,
    label: str(r.label) ?? "Passkey",
    kind: asKind(r.kind),
    createdAt,
    lastUsedAt: r.lastUsedAt != null || r.last_used_at != null
      ? asMs(r.lastUsedAt ?? r.last_used_at, createdAt)
      : undefined,
    thisDevice: r.thisDevice === true || r.this_device === true,
  };
}

export function parsePasskeys(rows: unknown): PasskeyCredential[] {
  if (!Array.isArray(rows)) return [];
  const out: PasskeyCredential[] = [];
  for (const row of rows) {
    const c = parsePasskey(row);
    if (c) out.push(c);
  }
  return out;
}

/** Sort passkeys: this device first, then most-recently-used (created if never
 *  used), newest first. Stable. */
export function sortPasskeys(creds: PasskeyCredential[]): PasskeyCredential[] {
  return [...creds].sort((a, b) => {
    if (Boolean(a.thisDevice) !== Boolean(b.thisDevice)) return a.thisDevice ? -1 : 1;
    return (b.lastUsedAt ?? b.createdAt) - (a.lastUsedAt ?? a.createdAt);
  });
}

/** A default label suggestion for a new passkey from the platform info. */
export function suggestPasskeyLabel(kind: AuthenticatorKind, platform?: string): string {
  if (kind === "platform") {
    if (platform && /mac|ios/i.test(platform)) return "Touch ID / Face ID";
    if (platform && /win/i.test(platform)) return "Windows Hello";
    return "This device";
  }
  if (kind === "cross-platform") return "Security key";
  return "Passkey";
}

// ---- Cached registry store ------------------------------------------------ //

export interface PasskeyRegistry {
  list(): PasskeyCredential[];
  set(creds: PasskeyCredential[]): void;
  /** Optimistically add (e.g. right after a successful create). */
  add(cred: PasskeyCredential): void;
  rename(id: string, label: string): void;
  remove(id: string): void;
  subscribe(fn: () => void): () => void;
}

export function createPasskeyRegistry(backing?: KeyValueStore | null): PasskeyRegistry {
  const store = resolveStore(backing);
  let creds = sortPasskeys(parsePasskeys(readJson<unknown[]>(store, CACHE_KEY, [])));
  const subs = new Set<() => void>();

  const commit = (next: PasskeyCredential[]) => {
    creds = sortPasskeys(next);
    writeJson(store, CACHE_KEY, creds);
    subs.forEach((fn) => fn());
  };

  return {
    list: () => creds,
    set: (next) => commit(next),
    add: (cred) => commit([...creds.filter((c) => c.id !== cred.id), cred]),
    rename: (id, label) =>
      commit(creds.map((c) => (c.id === id ? { ...c, label: label.trim() || c.label } : c))),
    remove: (id) => commit(creds.filter((c) => c.id !== id)),
    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}

export const PASSKEY_CACHE_KEY = CACHE_KEY;
