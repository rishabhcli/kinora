/**
 * Shared TanStack Query conventions: a single key factory (so desktop and mobile
 * cache/invalidate identically) and the default query options. The QueryClient
 * itself is created per-shell with `@tanstack/react-query` using these options.
 */
export const queryKeys = {
  me: () => ["me"] as const,
  books: () => ["books"] as const,
  book: (bookId: string) => ["book", bookId] as const,
  canon: (bookId: string) => ["book", bookId, "canon"] as const,
  shots: (bookId: string) => ["book", bookId, "shots"] as const,
  page: (bookId: string, page: number) => ["book", bookId, "page", page] as const,
  session: (sessionId: string) => ["session", sessionId] as const,
  /** The reader's learned directing style: global (no book) or per-book (§8.6). */
  directingStyle: (bookId?: string) =>
    bookId ? (["prefs", "book", bookId] as const) : (["prefs", "user"] as const),
  /** The cached §13 crew-vs-baseline eval report for a book. */
  evalReport: (bookId: string) => ["book", bookId, "eval-report"] as const,
  /** The live-recomputed §4.10 committed-buffer sawtooth for a session. */
  bufferTrace: (sessionId: string) => ["session", sessionId, "buffer-trace"] as const,
};

export const defaultQueryOptions = {
  staleTime: 30_000,
  retry: 1,
  refetchOnWindowFocus: false,
} as const;
