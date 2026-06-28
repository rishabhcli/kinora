// Recently-viewed ring buffer — a small, deduped MRU list of book ids, persisted
// through the same KeyValueStore seam as history. Powers the "Recent" section of
// the command palette and a "Jump back in" row. Pure logic over a store.
import type { KeyValueStore } from "./history";

const KEY = "kinora.discovery.recents.v1";
const CAP = 24;

function read(store: KeyValueStore): string[] {
  try {
    const raw = store.getItem(KEY);
    if (!raw) return [];
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export interface RecentsStore {
  list(): string[];
  /** Push an id to the front (MRU), dedup, cap. */
  push(id: string): void;
  clear(): void;
}

export function createRecentsStore(store: KeyValueStore, cap = CAP): RecentsStore {
  return {
    list: () => read(store),
    push(id) {
      const current = read(store).filter((x) => x !== id);
      current.unshift(id);
      store.setItem(KEY, JSON.stringify(current.slice(0, cap)));
    },
    clear() {
      store.setItem(KEY, "[]");
    },
  };
}

/** Resolve recent ids to books (preserving MRU order, skipping missing). */
export function resolveRecents<T extends { id: string }>(ids: string[], books: T[]): T[] {
  const byId = new Map(books.map((b) => [b.id, b]));
  const out: T[] = [];
  for (const id of ids) {
    const b = byId.get(id);
    if (b) out.push(b);
  }
  return out;
}
