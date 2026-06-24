/**
 * Keeps the library shelf live while books ingest — SSE + polling fallback.
 */
import {
  applyIngestProgress,
  booksNeedingSync,
  LibraryEventStream,
  type BookResponse,
  type EventSourceFactory,
  queryKeys,
} from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

export interface UseLibraryShelfSyncOptions {
  apiBaseUrl: string;
  getToken: () => Promise<string | null>;
  createEventSource?: EventSourceFactory;
  pollIntervalMs?: number;
  enabled?: boolean;
}

export function useLibraryShelfSync({
  apiBaseUrl,
  getToken,
  createEventSource,
  pollIntervalMs = 5_000,
  enabled = true,
}: UseLibraryShelfSyncOptions): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled || !createEventSource) return;
    const stream = new LibraryEventStream({
      apiBaseUrl,
      getToken,
      createEventSource,
      onProgress: ({ book_id, stage, pct }) => {
        queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (books) =>
          applyIngestProgress(books, book_id, { stage: stage ?? null, progress: pct ?? null }),
        );
      },
    });
    void stream.connect();
    return () => stream.close();
  }, [apiBaseUrl, createEventSource, enabled, getToken, queryClient]);

  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setInterval> | null = null;

    const maybePoll = () => {
      const books = queryClient.getQueryData<BookResponse[]>(queryKeys.books());
      if (!booksNeedingSync(books)) {
        if (timer !== null) {
          clearInterval(timer);
          timer = null;
        }
        return;
      }
      if (timer === null) {
        timer = setInterval(() => {
          void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
        }, pollIntervalMs);
      }
    };

    maybePoll();
    const unsub = queryClient.getQueryCache().subscribe(() => maybePoll());
    return () => {
      unsub();
      if (timer !== null) clearInterval(timer);
    };
  }, [enabled, pollIntervalMs, queryClient]);
}
