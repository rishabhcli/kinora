/**
 * Keeps the library shelf live while books ingest — polling fallback (mobile has no SSE).
 */
import { booksNeedingSync, type BookResponse, queryKeys } from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

export interface UseLibraryShelfSyncOptions {
  pollIntervalMs?: number;
  enabled?: boolean;
}

export function useLibraryShelfSync({
  pollIntervalMs = 5_000,
  enabled = true,
}: UseLibraryShelfSyncOptions = {}): void {
  const queryClient = useQueryClient();

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
