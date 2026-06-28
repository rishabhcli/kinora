// Library organization (Director domain) — faceted search, multi-key sorting,
// and *smart collections*: saved, rule-based shelves that re-evaluate as the
// library changes (e.g. "19th-century Romance, in progress"). All logic here is
// PURE + synchronously testable; persistence is a small injectable KV store so
// the same code drives the renderer and the tests with no DOM.
//
// This sits beside `lib/api/library.ts` (which owns the backend shelf load +
// the first-cut search/sort/shelf grouping). We deliberately keep the richer
// faceting here so library.ts stays a thin backend adapter.
import type { LibraryBook } from "./library";

// ---- Facets --------------------------------------------------------------- //

export type ReadingState = "unread" | "in_progress" | "finished";

/** Which reading bucket a book falls in, from its 0..100 progress. */
export function readingState(book: LibraryBook): ReadingState {
  if (book.progress <= 0) return "unread";
  if (book.progress >= 100) return "finished";
  return "in_progress";
}

/** A faceted-search query. Every field is optional; an absent field is "any". */
export interface FacetQuery {
  text?: string; // matches title OR author OR genre (case-insensitive substring)
  genres?: string[]; // OR within genres
  eras?: string[]; // OR within eras
  states?: ReadingState[]; // OR within reading states
  liveOnly?: boolean; // only backend-driven (generate-as-you-read) books
}

function matchesText(book: LibraryBook, text: string): boolean {
  const q = text.trim().toLowerCase();
  if (!q) return true;
  const hay = `${book.title} ${book.author} ${book.genre ?? ""} ${book.era ?? ""}`.toLowerCase();
  return q.split(/\s+/).every((tok) => hay.includes(tok));
}

/** Apply a faceted query. AND across facet *kinds*, OR within a kind — the
 *  conventional faceted-filter semantics. */
export function applyFacets(books: LibraryBook[], q: FacetQuery): LibraryBook[] {
  return books.filter((b) => {
    if (q.text && !matchesText(b, q.text)) return false;
    if (q.genres && q.genres.length && !(b.genre && q.genres.includes(b.genre))) return false;
    if (q.eras && q.eras.length && !(b.era && q.eras.includes(b.era))) return false;
    if (q.states && q.states.length && !q.states.includes(readingState(b))) return false;
    if (q.liveOnly && !b.live) return false;
    return true;
  });
}

/** A facet value + how many library books carry it (for the filter chips' counts). */
export interface FacetCount {
  value: string;
  count: number;
}

function countBy(books: LibraryBook[], pick: (b: LibraryBook) => string | undefined): FacetCount[] {
  const counts = new Map<string, number>();
  for (const b of books) {
    const v = pick(b);
    if (v) counts.set(v, (counts.get(v) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value));
}

/** The available facet values + counts, derived from the current library so the
 *  filter UI only shows facets that actually narrow the set. */
export interface FacetSummary {
  genres: FacetCount[];
  eras: FacetCount[];
  states: FacetCount[];
}

export function summarizeFacets(books: LibraryBook[]): FacetSummary {
  const stateLabel: Record<ReadingState, string> = {
    unread: "Unread",
    in_progress: "In progress",
    finished: "Finished",
  };
  const states = countBy(books, (b) => stateLabel[readingState(b)]);
  return {
    genres: countBy(books, (b) => b.genre),
    eras: countBy(books, (b) => b.era),
    states,
  };
}

// ---- Sorting -------------------------------------------------------------- //

export type SortField = "recent" | "title" | "author" | "progress" | "genre" | "era";
export type SortDir = "asc" | "desc";
export interface SortSpec {
  field: SortField;
  dir: SortDir;
}

function compareField(a: LibraryBook, b: LibraryBook, field: SortField): number {
  switch (field) {
    case "title":
      return a.title.localeCompare(b.title);
    case "author":
      return a.author.localeCompare(b.author);
    case "progress":
      return a.progress - b.progress;
    case "genre":
      return (a.genre ?? "~").localeCompare(b.genre ?? "~");
    case "era":
      return (a.era ?? "~").localeCompare(b.era ?? "~");
    case "recent":
    default:
      return 0; // input is already newest-first; "recent asc" keeps it
  }
}

/** Stable multi-key sort. Books compare by each spec in order; ties fall through
 *  to the next key, then to the original (newest-first) order for stability. */
export function sortBySpecs(books: LibraryBook[], specs: SortSpec[]): LibraryBook[] {
  const indexed = books.map((book, i) => ({ book, i }));
  indexed.sort((x, y) => {
    for (const spec of specs) {
      const cmp = compareField(x.book, y.book, spec.field);
      if (cmp !== 0) return spec.dir === "desc" ? -cmp : cmp;
    }
    return x.i - y.i;
  });
  return indexed.map(({ book }) => book);
}

// ---- Smart collections ---------------------------------------------------- //

/** A saved, rule-based shelf. The rules ARE a `FacetQuery` plus an ordering, so
 *  a collection re-evaluates itself against the live library every render. */
export interface SmartCollection {
  id: string;
  name: string;
  query: FacetQuery;
  sort: SortSpec[];
  /** Optional emoji/glyph the shelf header shows. */
  icon?: string;
  /** Pinned collections sort to the top of the rail; createdAt breaks ties. */
  pinned?: boolean;
  createdAt: number;
}

/** Evaluate a smart collection against the library → its current members. */
export function evaluateCollection(
  books: LibraryBook[],
  collection: SmartCollection,
): LibraryBook[] {
  const filtered = applyFacets(books, collection.query);
  return collection.sort.length ? sortBySpecs(filtered, collection.sort) : filtered;
}

/** Built-in starter collections every library gets until the user makes their
 *  own. They re-evaluate live, so an empty library just shows empty shelves. */
export function defaultCollections(): SmartCollection[] {
  const t = 0; // deterministic createdAt for the built-ins (stable order)
  return [
    {
      id: "builtin:in-progress",
      name: "Continue Reading",
      icon: "▶",
      query: { states: ["in_progress"] },
      sort: [{ field: "progress", dir: "desc" }],
      pinned: true,
      createdAt: t,
    },
    {
      id: "builtin:unread",
      name: "Up Next",
      icon: "✧",
      query: { states: ["unread"] },
      sort: [{ field: "recent", dir: "asc" }],
      createdAt: t + 1,
    },
    {
      id: "builtin:finished",
      name: "Finished",
      icon: "✓",
      query: { states: ["finished"] },
      sort: [{ field: "title", dir: "asc" }],
      createdAt: t + 2,
    },
    {
      id: "builtin:live",
      name: "Live Films",
      icon: "◉",
      query: { liveOnly: true },
      sort: [{ field: "recent", dir: "asc" }],
      createdAt: t + 3,
    },
  ];
}

const BUILTIN_PREFIX = "builtin:";
export function isBuiltinCollection(c: SmartCollection): boolean {
  return c.id.startsWith(BUILTIN_PREFIX);
}

/** Order collections for the rail: pinned first, then by createdAt (built-ins
 *  before user ones, since built-ins use t∈[0,3]). */
export function orderedCollections(collections: SmartCollection[]): SmartCollection[] {
  return [...collections].sort((a, b) => {
    if (Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1;
    return a.createdAt - b.createdAt;
  });
}

// ---- Persistence (injectable KV — mirrors lib/settings.ts conventions) ---- //

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const STORAGE_KEY = "kinora.collections.v1";

function browserStore(): KeyValueStore | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    /* storage unavailable */
  }
  return null;
}

function isFacetQuery(v: unknown): v is FacetQuery {
  return typeof v === "object" && v !== null;
}

/** Validate a persisted blob back into SmartCollections, dropping malformed
 *  rows. User collections only — built-ins are re-derived, never persisted. */
function parseCollections(raw: string | null): SmartCollection[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: SmartCollection[] = [];
  for (const row of parsed) {
    if (typeof row !== "object" || row === null) continue;
    const r = row as Record<string, unknown>;
    if (typeof r.id !== "string" || typeof r.name !== "string") continue;
    if (!isFacetQuery(r.query)) continue;
    out.push({
      id: r.id,
      name: r.name,
      query: r.query as FacetQuery,
      sort: Array.isArray(r.sort) ? (r.sort as SortSpec[]) : [],
      icon: typeof r.icon === "string" ? r.icon : undefined,
      pinned: r.pinned === true,
      createdAt: typeof r.createdAt === "number" ? r.createdAt : 0,
    });
  }
  return out;
}

export interface CollectionStore {
  /** Built-ins + persisted user collections, in rail order. */
  list(): SmartCollection[];
  /** The user (non-built-in) collections only. */
  userCollections(): SmartCollection[];
  /** Add or replace a user collection (built-ins are rejected). Returns the saved row. */
  upsert(c: Omit<SmartCollection, "createdAt"> & { createdAt?: number }): SmartCollection;
  /** Remove a user collection by id (built-ins are ignored). */
  remove(id: string): void;
  subscribe(fn: () => void): () => void;
}

/** A persisted store of user smart-collections. Built-ins are always present and
 *  never written. Notifies subscribers on every mutation. */
export function createCollectionStore(backing?: KeyValueStore): CollectionStore {
  const store = backing ?? browserStore();
  let user: SmartCollection[] = parseCollections(store ? store.getItem(STORAGE_KEY) : null);
  const subs = new Set<() => void>();

  const persist = () => {
    try {
      store?.setItem(STORAGE_KEY, JSON.stringify(user));
    } catch {
      /* storage write blocked — keep the in-memory copy */
    }
    subs.forEach((fn) => fn());
  };

  return {
    list: () => orderedCollections([...defaultCollections(), ...user]),
    userCollections: () => orderedCollections(user),
    upsert(c) {
      if (isBuiltinCollection(c as SmartCollection)) {
        throw new Error("built-in collections cannot be edited");
      }
      const createdAt = c.createdAt ?? Date.now();
      const next: SmartCollection = { ...c, createdAt };
      user = [...user.filter((x) => x.id !== next.id), next];
      persist();
      return next;
    },
    remove(id) {
      if (id.startsWith(BUILTIN_PREFIX)) return;
      const before = user.length;
      user = user.filter((x) => x.id !== id);
      if (user.length !== before) persist();
    },
    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}
