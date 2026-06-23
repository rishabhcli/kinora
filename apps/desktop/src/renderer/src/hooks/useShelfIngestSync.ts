import {
  applyIngestProgress,
  INGEST_POLL_MS,
  LibraryEventStream,
  queryKeys,
  shelfHasImporting,
  type BookResponse,
} from "@kinora/core";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

/** Patch the books list cache with a live ingest_progress event. */
function patchBooksCache(queryClient: QueryClient, raw: unknown): void {
  queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (books) => applyIngestProgress(books, raw));
}

/**
 * Keep the shelf's import status live: SSE for stage/pct updates while Phase A
 * runs, plus a short poll until every book flips to ready/failed (the backend
 * does not emit a terminal SSE when ingest completes).
 */
export function useShelfIngestSync(hasImporting: boolean): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (typeof EventSource === "undefined") return undefined;
    const stream = new LibraryEventStream({
      baseUrl: API_BASE_URL,
      getToken: () => authStore.getState().token,
      createEventSource: (url) => new EventSource(url),
      onIngestProgress: (event) => patchBooksCache(queryClient, event),
      reconnect: true,
    });
    void stream.connect();
    return () => stream.close();
  }, [queryClient]);

  useEffect(() => {
    if (!hasImporting) return undefined;
    const timer = window.setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
    }, INGEST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [hasImporting, queryClient]);
}

export { shelfHasImporting };
