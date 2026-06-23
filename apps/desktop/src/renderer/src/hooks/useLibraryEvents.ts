import {
  applyIngestProgress,
  LibraryEventStream,
  patchBooksWithIngest,
  queryKeys,
  uploadErrorMessage,
  type BookResponse,
} from "@kinora/core";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

function patchShelfCache(
  queryClient: QueryClient,
  patch: { book_id: string; stage?: string; pct?: number },
): void {
  queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (books) =>
    patchBooksWithIngest(books, patch),
  );
  queryClient.setQueryData<BookResponse>(queryKeys.book(patch.book_id), (book) =>
    book ? applyIngestProgress(book, patch) : book,
  );
  const stage = patch.stage;
  if (stage === "ready" || stage === "failed") {
    void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
    void queryClient.invalidateQueries({ queryKey: queryKeys.book(patch.book_id) });
  }
}

/** Subscribe to live shelf ingest progress and patch the React Query cache. */
export function useLibraryEvents(enabled: boolean): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) return;
    const stream = new LibraryEventStream({
      baseUrl: API_BASE_URL,
      getToken: async () => authStore.getState().token,
      onIngestProgress: (event) => {
        patchShelfCache(queryClient, {
          book_id: event.book_id,
          stage: event.stage,
          pct: event.pct,
        });
      },
    });
    void stream.connect();
    return () => stream.close();
  }, [enabled, queryClient]);
}
