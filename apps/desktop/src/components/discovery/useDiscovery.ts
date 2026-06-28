// useDiscovery — the React glue that wires the pure discovery cores to component
// state. Owns the persisted interaction history + recents stores, derives the
// taste profile + personalized rows, and exposes a stable `record()` callback so
// any surface (cards, palette) can log a signal that feeds future recs.
//
// All heavy logic lives in lib/discovery/*; this hook is intentionally thin and
// memoized so re-renders are cheap on a long catalog.
import { useCallback, useMemo, useRef, useState } from "react";
import type {
  DiscoveryBook,
  DiscoveryRow,
  Interaction,
  InteractionKind,
  TasteProfile,
} from "../../lib/discovery/types";
import {
  createHistoryStore,
  browserStore,
  type HistoryStore,
  type KeyValueStore,
} from "../../lib/discovery/history";
import { createRecentsStore, type RecentsStore } from "../../lib/discovery/recents";
import { buildProfile } from "../../lib/discovery/affinity";
import { buildRows } from "../../lib/discovery/rows";

export interface UseDiscoveryOptions {
  /** Injectable store seam for tests (defaults to localStorage-backed). */
  store?: KeyValueStore;
  /** Popularity prior by book id (0..1). */
  popularity?: Record<string, number>;
  /** Injectable clock for deterministic tests. */
  now?: () => number;
}

export interface DiscoveryApi {
  rows: DiscoveryRow[];
  profile: TasteProfile;
  history: Interaction[];
  recents: string[];
  /** Log an interaction; updates history + recents + (on the next render) recs. */
  record: (book: DiscoveryBook, kind: InteractionKind) => void;
  /** Mark "not interested" — a dismiss signal that excludes the book from recs. */
  dismiss: (book: DiscoveryBook) => void;
  /** Reset all learned taste. */
  reset: () => void;
}

export function useDiscovery(
  books: DiscoveryBook[],
  opts: UseDiscoveryOptions = {},
): DiscoveryApi {
  const now = opts.now;
  // Stores are created once; the seam is stable across renders.
  const historyStore = useRef<HistoryStore>();
  const recentsStore = useRef<RecentsStore>();
  if (!historyStore.current) {
    const seam = opts.store ?? browserStore();
    historyStore.current = createHistoryStore(seam, now ? { now } : {});
    recentsStore.current = createRecentsStore(seam);
  }

  // A monotonically-increasing token forces a recompute when history mutates
  // (the stores write to localStorage, which React can't observe directly).
  const [version, setVersion] = useState(0);

  const record = useCallback(
    (book: DiscoveryBook, kind: InteractionKind) => {
      historyStore.current!.record(book.id, kind, {
        genre: book.genre,
        era: book.era,
        author: book.author,
      });
      if (kind === "open" || kind === "preview" || kind === "finish") {
        recentsStore.current!.push(book.id);
      }
      setVersion((v) => v + 1);
    },
    [],
  );

  const dismiss = useCallback(
    (book: DiscoveryBook) => {
      historyStore.current!.record(book.id, "dismiss", {
        genre: book.genre,
        era: book.era,
        author: book.author,
      });
      setVersion((v) => v + 1);
    },
    [],
  );

  const reset = useCallback(() => {
    historyStore.current!.clear();
    recentsStore.current!.clear();
    setVersion((v) => v + 1);
  }, []);

  const history = useMemo(() => historyStore.current!.all(), [version]);
  const recents = useMemo(() => recentsStore.current!.list(), [version]);

  const profile = useMemo(
    () => buildProfile(history, { now: now ? now() : undefined }),
    [history, now],
  );

  const rows = useMemo(
    () =>
      buildRows(books, {
        profile,
        history,
        popularity: opts.popularity,
        now: now ? now() : undefined,
      }),
    [books, profile, history, opts.popularity, now],
  );

  return { rows, profile, history, recents, record, dismiss, reset };
}
