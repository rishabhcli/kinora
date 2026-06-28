// Shared persistence seam for the account domain. Mirrors the injectable KV
// convention used by lib/settings.ts, lib/api/collections.ts and
// lib/api/analytics.ts: a tiny synchronous get/set interface so the pure core
// is testable with an in-memory map and the renderer wires localStorage.
//
// Everything in lib/account/* takes a `KeyValueStore` (or falls back to the
// browser one) — there is no implicit global. That is what keeps the logic
// DOM-free and deterministic in tests.

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

/** A pluggable byte source so secret/token generation is deterministic in tests.
 *  The renderer wires crypto.getRandomValues; the insecure fallback is for
 *  SSR/tests only (real secrets always come from the backend). Shared by the
 *  mfa + oauth modules so the barrel exports one definition. */
export type RandomBytes = (n: number) => Uint8Array;

/** NOT cryptographically secure — fallback + test source only. */
export const insecureRandomBytes: RandomBytes = (n) => {
  const out = new Uint8Array(n);
  for (let i = 0; i < n; i++) out[i] = Math.floor(Math.random() * 256);
  return out;
};

/** crypto.getRandomValues if available, else the insecure fallback. */
export function webRandomBytes(): RandomBytes {
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (c && typeof c.getRandomValues === "function") {
    return (n) => c.getRandomValues(new Uint8Array(n));
  }
  return insecureRandomBytes;
}

/** The renderer-side localStorage, or null when storage is unavailable
 *  (SSR, a hardened renderer, private-mode quota). Callers degrade to an
 *  in-memory copy. */
export function browserStore(): KeyValueStore | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    /* storage unavailable in this renderer */
  }
  return null;
}

/** A throwaway in-memory store — used as the fallback when there is no browser
 *  storage, and directly by tests. Honours the optional removeItem. */
export function memoryStore(seed?: Record<string, string>): KeyValueStore {
  const map = new Map<string, string>(Object.entries(seed ?? {}));
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

/** Resolve the store to use: the caller's, else the browser's, else memory. */
export function resolveStore(backing?: KeyValueStore | null): KeyValueStore {
  return backing ?? browserStore() ?? memoryStore();
}

/** Read + JSON.parse a key, returning `fallback` on missing/corrupt data.
 *  Never throws — corrupt persisted state should degrade, not crash the app. */
export function readJson<T>(store: KeyValueStore, key: string, fallback: T): T {
  let raw: string | null;
  try {
    raw = store.getItem(key);
  } catch {
    return fallback;
  }
  if (raw == null) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/** JSON.stringify + write a key. Swallows quota/permission errors (the caller
 *  keeps its in-memory copy). Returns whether the write succeeded. */
export function writeJson(store: KeyValueStore, key: string, value: unknown): boolean {
  try {
    store.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

/** Remove a key, tolerating stores without removeItem (write empty instead). */
export function removeKey(store: KeyValueStore, key: string): void {
  try {
    if (store.removeItem) store.removeItem(key);
    else store.setItem(key, "");
  } catch {
    /* ignore */
  }
}
