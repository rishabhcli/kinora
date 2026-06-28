// Interaction-history store — the persisted log the taste profile is built from.
// Storage goes through a tiny KeyValueStore seam (same shape lib/api/collections
// uses) so it's deterministic in tests and backend-agnostic in the app.
import type { Interaction, InteractionKind } from "./types";

/** The minimal storage seam: synchronous get/set of strings. localStorage
 *  satisfies it directly; tests inject an in-memory map. */
export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const STORAGE_KEY = "kinora.discovery.history.v1";
const MAX_RECORDS = 500; // ring buffer — keep history bounded

/** A localStorage-backed store, falling back to an in-memory map when storage is
 *  unavailable (SSR / blocked / Electron edge cases). */
export function browserStore(): KeyValueStore {
  try {
    if (typeof localStorage !== "undefined") {
      // probe (Safari private mode throws on setItem)
      const probe = "__kinora_probe__";
      localStorage.setItem(probe, "1");
      localStorage.removeItem(probe);
      return localStorage;
    }
  } catch {
    /* fall through to memory */
  }
  const mem = new Map<string, string>();
  return { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => void mem.set(k, v) };
}

function safeParse(raw: string | null): Interaction[] {
  if (!raw) return [];
  try {
    const data = JSON.parse(raw);
    if (!Array.isArray(data)) return [];
    return data.filter(
      (r): r is Interaction =>
        r && typeof r.bookId === "string" && typeof r.kind === "string" && typeof r.at === "number",
    );
  } catch {
    return [];
  }
}

export interface HistoryStore {
  /** All interactions, oldest-first. */
  all(): Interaction[];
  /** Append one interaction (auto-trims to the ring-buffer cap). */
  record(
    bookId: string,
    kind: InteractionKind,
    meta?: { genre?: string; era?: string; author?: string },
    at?: number,
  ): void;
  /** Interactions for a single book, oldest-first. */
  forBook(bookId: string): Interaction[];
  /** The most recent interaction of any kind for a book, or null. */
  lastFor(bookId: string): Interaction | null;
  /** Clear everything (logout / "reset recommendations"). */
  clear(): void;
}

export function createHistoryStore(
  store: KeyValueStore,
  opts: { now?: () => number; max?: number } = {},
): HistoryStore {
  const now = opts.now ?? (() => Date.now());
  const max = opts.max ?? MAX_RECORDS;

  function load(): Interaction[] {
    return safeParse(store.getItem(STORAGE_KEY));
  }
  function save(records: Interaction[]): void {
    const trimmed = records.length > max ? records.slice(records.length - max) : records;
    store.setItem(STORAGE_KEY, JSON.stringify(trimmed));
  }

  return {
    all: () => load(),
    record(bookId, kind, meta = {}, at) {
      const records = load();
      records.push({ bookId, kind, at: at ?? now(), ...meta });
      save(records);
    },
    forBook(bookId) {
      return load().filter((r) => r.bookId === bookId);
    },
    lastFor(bookId) {
      const mine = load().filter((r) => r.bookId === bookId);
      return mine.length ? mine[mine.length - 1] : null;
    },
    clear() {
      store.setItem(STORAGE_KEY, "[]");
    },
  };
}
