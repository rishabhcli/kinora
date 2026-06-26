// Vitest global setup: jest-dom matchers + automatic React Testing Library
// cleanup between tests. Importing the `/vitest` entry also augments vitest's
// `expect` types, so matchers like `.toBeInTheDocument()` typecheck project-wide.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom does not implement the Web Storage API, so provide an in-memory shim
// (the app persists reading prefs + the reduced-motion override to localStorage).
class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length(): number {
    return this.store.size;
  }
  clear(): void {
    this.store.clear();
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

// Define unconditionally (don't read the global first — Node's experimental
// localStorage getter warns when accessed without --localstorage-file).
try {
  Object.defineProperty(globalThis, "localStorage", {
    value: new MemoryStorage(),
    writable: true,
    configurable: true,
  });
} catch {
  /* a real, locked localStorage already exists */
}

afterEach(() => {
  cleanup();
});
