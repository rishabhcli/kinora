// Reading taste (account domain) — the small set of genres a reader picks
// during onboarding to seed recommendations. A pure catalog + a persisted,
// validated selection set over an injectable store. Kept here (not in the
// library domain, which another agent owns) because it's an account-level
// preference captured at sign-up and reused across sessions.
import { type KeyValueStore, readJson, resolveStore, writeJson } from "./store";

/** The genres offered at onboarding. Display order is curated. */
export const TASTE_GENRES = [
  "Literary Fiction",
  "Science Fiction",
  "Fantasy",
  "Mystery & Thriller",
  "Romance",
  "Historical",
  "Horror",
  "Adventure",
  "Poetry",
  "Biography",
  "Philosophy",
  "Classics",
] as const;

export type TasteGenre = (typeof TASTE_GENRES)[number];

const GENRE_SET = new Set<string>(TASTE_GENRES);
const STORAGE_KEY = "kinora.account.taste.v1";
const MAX_SELECTION = 8;

/** Validate a persisted blob into a clean, de-duplicated genre list. */
export function parseTaste(raw: unknown): TasteGenre[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const out: TasteGenre[] = [];
  for (const g of raw) {
    if (typeof g === "string" && GENRE_SET.has(g) && !seen.has(g)) {
      seen.add(g);
      out.push(g as TasteGenre);
    }
  }
  return out.slice(0, MAX_SELECTION);
}

/** Toggle a genre in/out of the selection, honouring the cap. Pure. */
export function toggleGenre(selected: TasteGenre[], genre: TasteGenre): TasteGenre[] {
  if (selected.includes(genre)) return selected.filter((g) => g !== genre);
  if (selected.length >= MAX_SELECTION) return selected; // at cap — ignore
  return [...selected, genre];
}

export interface TasteStore {
  get(): TasteGenre[];
  set(genres: TasteGenre[]): void;
  toggle(genre: TasteGenre): TasteGenre[];
  subscribe(fn: () => void): () => void;
}

export function createTasteStore(backing?: KeyValueStore | null): TasteStore {
  const store = resolveStore(backing);
  let selected = parseTaste(readJson<unknown>(store, STORAGE_KEY, []));
  const subs = new Set<() => void>();

  const commit = (next: TasteGenre[]) => {
    selected = next;
    writeJson(store, STORAGE_KEY, selected);
    subs.forEach((fn) => fn());
    return selected;
  };

  return {
    get: () => selected,
    set: (genres) => void commit(parseTaste(genres)),
    toggle: (genre) => commit(toggleGenre(selected, genre)),
    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}

export const TASTE_STORAGE_KEY = STORAGE_KEY;
export const TASTE_MAX_SELECTION = MAX_SELECTION;
