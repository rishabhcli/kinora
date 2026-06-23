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
};

export const defaultQueryOptions = {
  staleTime: 30_000,
  retry: 1,
  refetchOnWindowFocus: false,
} as const;
