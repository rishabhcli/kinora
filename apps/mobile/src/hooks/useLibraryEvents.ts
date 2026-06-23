import {
  applyIngestProgress,
  LibraryEventStream,
  queryKeys,
  shelfNeedsIngestUpdates,
  type BookResponse,
  type KinoraEvent,
} from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

const POLL_MS = 5_000;

/** Live ingest updates for the mobile library shelf (SSE + poll fallback). */
export function useLibraryEvents(enabled: boolean): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) return;

    function patchBooks(updater: (books: BookResponse[]) => BookResponse[]): void {
      queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (prev) => {
        if (!prev) return prev;
        return updater(prev);
      });
    }

    function onEvent(event: KinoraEvent): void {
      if (event.event !== "ingest_progress") return;
      patchBooks((books) =>
        books.map((book) =>
          book.id === event.book_id
            ? applyIngestProgress(book, { stage: event.stage, pct: event.pct })
            : book,
        ),
      );
      if (event.pct != null && event.pct >= 1) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
      }
    }

    const stream = new LibraryEventStream({
      baseUrl: API_BASE_URL,
      getToken: async () => authStore.getState().token,
      // React Native has no EventSource — the poll fallback keeps mobile honest.
      createEventSource: (url) => {
        if (typeof EventSource !== "undefined") {
          return new EventSource(url) as unknown as import("@kinora/core").EventSourceLike;
        }
        return {
          close() {},
          onopen: null,
          onerror: null,
          onmessage: null,
        };
      },
      onEvent,
      reconnect: typeof EventSource !== "undefined",
    });
    void stream.connect();
    return () => stream.close();
  }, [enabled, queryClient]);
}

export function libraryBooksQueryOptions() {
  return {
    refetchInterval: (query: { state: { data?: BookResponse[] } }) =>
      shelfNeedsIngestUpdates(query.state.data) ? POLL_MS : false,
  } as const;
}
