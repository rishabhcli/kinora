import {
  LibraryEventsClient,
  patchBooksWithIngestProgress,
  queryKeys,
  shelfHasPendingImports,
  type BookResponse,
  type EventSourceLike,
} from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

const IMPORT_POLL_MS = 4_000;

/** Live ingest progress for the mobile shelf (polling + SSE when available). */
export function useLibraryEvents(books: BookResponse[] | undefined): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    const applyProgress = (payload: Parameters<typeof patchBooksWithIngestProgress>[1]) => {
      queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (old) => {
        if (!old) return old;
        return patchBooksWithIngestProgress(old, payload);
      });
      if (payload.stage === "ready" || payload.stage === "failed") {
        void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
      }
    };

    const client = new LibraryEventsClient({
      baseUrl: API_BASE_URL,
      getToken: async () => authStore.getState().token,
      createEventSource:
        typeof EventSource !== "undefined"
          ? (url) => new EventSource(url) as unknown as EventSourceLike
          : undefined,
      onProgress: applyProgress,
    });
    void client.connect();
    return () => client.close();
  }, [queryClient]);

  useEffect(() => {
    if (!shelfHasPendingImports(books)) return;
    const timer = setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
    }, IMPORT_POLL_MS);
    return () => clearInterval(timer);
  }, [books, queryClient]);
}
