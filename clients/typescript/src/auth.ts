/**
 * Token storage abstraction for the Kinora SDK.
 *
 * The SDK never assumes a browser: by default the token lives in memory. A
 * caller can plug in any store (localStorage, a keychain, a file) by passing an
 * object with `get`/`set`. The renderer's `apps/desktop/src/lib/api.ts` keeps
 * the token in localStorage under `kinora.token`; {@link browserTokenStore}
 * reproduces that behaviour for parity when running in a browser.
 */

export interface TokenStore {
  get(): string | null | undefined;
  set(token: string | null): void;
}

/** A simple in-memory token store (the default). */
export class MemoryTokenStore implements TokenStore {
  private token: string | null = null;
  constructor(initial: string | null = null) {
    this.token = initial;
  }
  get(): string | null {
    return this.token;
  }
  set(token: string | null): void {
    this.token = token;
  }
}

/**
 * A browser token store backed by `localStorage` (falling back to memory when
 * storage is unavailable), matching the renderer client's `kinora.token` key.
 */
export function browserTokenStore(key = "kinora.token"): TokenStore {
  const memory = new MemoryTokenStore();
  const ls = (): Storage | null => {
    try {
      return typeof globalThis !== "undefined" && "localStorage" in globalThis
        ? (globalThis as { localStorage: Storage }).localStorage
        : null;
    } catch {
      return null;
    }
  };
  return {
    get(): string | null | undefined {
      try {
        const v = ls()?.getItem(key);
        if (v) return v;
      } catch {
        /* blocked */
      }
      return memory.get();
    },
    set(token: string | null): void {
      memory.set(token);
      try {
        const store = ls();
        if (!store) return;
        if (token) store.setItem(key, token);
        else store.removeItem(key);
      } catch {
        /* blocked */
      }
    },
  };
}
