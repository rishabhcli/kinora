import {
  LibraryEventsSource,
  patchBooksWithIngestEvent,
  queryKeys,
  shelfHasImporting,
  type BookResponse,
  type KinoraEvent,
} from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { useAuth } from "./useAuth";
import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

/**
 * Subscribe to `GET /api/books/events` while signed in and patch the shelf cache
 * with live Phase-A ingest progress. Refetches when a book finishes importing.
 */
export function useLibraryIngestEvents() {
  const token = useAuth((state) => state.token);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!token) return;

    const source = new LibraryEventsSource({
      baseUrl: API_BASE_URL,
      getToken: async () => authStore.getState().token,
      createEventSource: (url) => new EventSource(url) as unknown as import("@kinora/core").EventSourceLike,
      onEvent: (event: KinoraEvent) => {
        if (event.event !== "ingest_progress") return;
        queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (rows) =>
          patchBooksWithIngestEvent(rows, event) ?? rows,
        );
        if (typeof event.pct === "number" && event.pct >= 1) {
          void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
        }
      },
    });

    void source.connect();
    return () => source.close();
  }, [token, queryClient]);

  useEffect(() => {
    if (!token) return;
    const id = setInterval(() => {
      const books = queryClient.getQueryData<BookResponse[]>(queryKeys.books());
      if (shelfHasImporting(books)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
      }
    }, 12_000);
    return () => clearInterval(id);
  }, [token, queryClient]);
}
