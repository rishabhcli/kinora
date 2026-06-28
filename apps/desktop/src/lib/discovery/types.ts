// Discovery engine — shared types. The discovery surface (home shell) consumes
// the renderer's `Book` shape (data/books.ts, also produced by lib/api.toUiBook)
// and layers taste/recommendation signals on top of it WITHOUT modifying it.
import type { Book } from "../../data/books";

/** A book as the discovery engine sees it: the renderer `Book` plus the optional
 *  catalogue metadata (genre/era) that the library client (`lib/api/library.ts`)
 *  already attaches. We re-declare a structural superset here so the discovery
 *  cores don't depend on the library agent's module. */
export interface DiscoveryBook extends Book {
  genre?: string;
  era?: string;
}

/** A reader interaction with a book, used to build the taste profile. The weight
 *  encodes how strong a signal each kind of event is (an open is worth far more
 *  than a hover). `at` is an epoch-ms timestamp so recency can decay. */
export type InteractionKind =
  | "view" // surfaced/impression (weak)
  | "hover" // hovered a card (weak)
  | "preview" // opened the rich preview (medium)
  | "open" // opened the reading room (strong)
  | "finish" // finished the book (strongest positive)
  | "favorite" // hearted (strong)
  | "dismiss"; // "not interested" (strong negative)

export interface Interaction {
  bookId: string;
  kind: InteractionKind;
  at: number; // epoch ms
  /** Denormalized so a profile can be built without joining back to the catalog
   *  (the catalog can change; history persists). Optional for older records. */
  genre?: string;
  era?: string;
  author?: string;
}

/** The reader's learned taste, derived purely from interaction history. Scores
 *  are unnormalized weights (higher = stronger affinity); negatives mean the
 *  reader has actively dismissed that dimension. */
export interface TasteProfile {
  genres: Record<string, number>;
  eras: Record<string, number>;
  authors: Record<string, number>;
  /** Book ids the reader has explicitly dismissed — excluded from recs. */
  dismissed: Set<string>;
  /** Total positive signal — used to decide "cold start" (no taste yet). */
  totalSignal: number;
}

/** A single facet (search/filter dimension) and its available values + counts. */
export interface Facet {
  key: "genre" | "era" | "author" | "state";
  label: string;
  values: { value: string; count: number }[];
}

/** Active facet selections for faceted search. Empty arrays = unconstrained. */
export interface FacetSelection {
  text?: string;
  genre?: string[];
  era?: string[];
  author?: string[];
  /** Reading state buckets: unread | reading | finished. */
  state?: ReadingState[];
}

export type ReadingState = "unread" | "reading" | "finished";

/** A ranked search hit, carrying the relevance score + which fields matched (so
 *  the UI can highlight). */
export interface SearchHit {
  book: DiscoveryBook;
  score: number;
  matchedFields: ("title" | "author" | "genre" | "era")[];
}

/** A scored recommendation with a human-readable reason ("Because you read …"). */
export interface Recommendation {
  book: DiscoveryBook;
  score: number;
  reason: string;
  /** The dominant signal that drove the score, for grouping into rows. */
  basis: "genre" | "author" | "era" | "popular" | "continue" | "new" | "fresh";
}

/** A generated home row (Netflix-style shelf). */
export interface DiscoveryRow {
  id: string;
  title: string;
  books: DiscoveryBook[];
  /** Why this row exists — drives the subtitle ("Because you read Dune"). */
  reason?: string;
  kind: "continue" | "recommended" | "genre" | "new" | "popular" | "rediscover" | "library";
}
