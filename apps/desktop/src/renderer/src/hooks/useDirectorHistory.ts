import { useCallback, useEffect, useMemo, useState } from "react";

/** One direction the reader gave a shot (§5.4) — the note + how it routed. */
export interface DirectionEntry {
  note: string;
  agent: string;
  aspect: string;
  /** ms since epoch. */
  at: number;
}

export interface DirectorHistory {
  /** Per-shot direction log (newest first). */
  byShot: Record<string, DirectionEntry[]>;
  /** shotId → number of directions given, for tile badges. */
  counts: Record<string, number>;
  /** Record a sent direction against a shot (persists). */
  record: (shotId: string, entry: DirectionEntry) => void;
  /** The newest directions for one shot (capped). */
  recentFor: (shotId: string | null) => DirectionEntry[];
}

const STORE_KEY = "kinora.director.history.v1";
const MAX_PER_SHOT = 12;

type Store = Record<string, Record<string, DirectionEntry[]>>; // bookId -> shotId -> entries

function loadStore(): Store {
  if (typeof localStorage === "undefined") return {};
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) ?? "{}");
    return typeof raw === "object" && raw !== null ? (raw as Store) : {};
  } catch {
    return {};
  }
}

/**
 * The reader's directing history per shot, kept on the client and persisted per
 * book (§5.4). Powers the "this shot has N directions" tile badges and the
 * recent-directions list in the composer, so the Director sees what they've
 * already asked of a shot — a lightweight, local complement to the §9.6 server
 * preference write-back.
 */
export function useDirectorHistory(bookId: string | null): DirectorHistory {
  const [store, setStore] = useState<Store>(loadStore);

  // Pick up edits made under another book key without clobbering them.
  useEffect(() => {
    setStore(loadStore());
  }, [bookId]);

  const byShot = useMemo(() => (bookId ? (store[bookId] ?? {}) : {}), [store, bookId]);

  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const [shotId, entries] of Object.entries(byShot)) out[shotId] = entries.length;
    return out;
  }, [byShot]);

  const record = useCallback(
    (shotId: string, entry: DirectionEntry) => {
      if (!bookId) return;
      setStore((prev) => {
        const book = prev[bookId] ?? {};
        const shot = [entry, ...(book[shotId] ?? [])].slice(0, MAX_PER_SHOT);
        const next: Store = { ...prev, [bookId]: { ...book, [shotId]: shot } };
        try {
          localStorage.setItem(STORE_KEY, JSON.stringify(next));
        } catch {
          /* private mode — keep in memory */
        }
        return next;
      });
    },
    [bookId],
  );

  const recentFor = useCallback(
    (shotId: string | null): DirectionEntry[] => (shotId ? (byShot[shotId] ?? []) : []),
    [byShot],
  );

  return { byShot, counts, record, recentFor };
}
