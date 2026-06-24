import {
  applyIngestProgress,
  hasImportingBooks,
  IMPORT_POLL_MS,
  LibraryEventStream,
  queryKeys,
  type BookResponse,
  type EventSourceLike,
  type KinoraEvent,
} from "@kinora/core";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { API_BASE_URL } from "../lib/config";
import { authStore } from "../lib/auth";

function patchBooksCache(queryClient: QueryClient, event: KinoraEvent): void {
  if (event.event !== "ingest_progress") return;
  queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (prev) => {
    if (!prev) return prev;
    return prev.map((book) => applyIngestProgress(book, event));
  });
  const stage = typeof event.stage === "string" ? event.stage : null;
  if (stage === "ready" || stage === "failed") {
    void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
  }
}

/**
 * Subscribe to live shelf ingest progress (SSE) and poll as a fallback while
 * any book is still importing.
 */
export function useLibraryShelfSync(books: BookResponse[] | undefined, enabled: boolean): void {
  const queryClient = useQueryClient();
  const importing = enabled && books != null && hasImportingBooks(books);

  useEffect(() => {
    if (!enabled) return;
    const stream = new LibraryEventStream({
      baseUrl: API_BASE_URL,
      getToken: async () => authStore.getState().token,
      createEventSource: (url) => new EventSource(url) as unknown as EventSourceLike,
      onEvent: (event) => patchBooksCache(queryClient, event),
    });
    void stream.connect();
    return () => stream.close();
  }, [enabled, queryClient]);

  useEffect(() => {
    if (!importing) return;
    const timer = setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
    }, IMPORT_POLL_MS);
    return () => clearInterval(timer);
  }, [importing, queryClient]);
}
