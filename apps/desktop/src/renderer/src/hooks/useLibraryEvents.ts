import {
  applyIngestProgress,
  isTerminalIngest,
  parseIngestProgress,
  queryKeys,
  type BookResponse,
} from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

/**
 * Subscribe to `GET /api/books/events` and keep the shelf cache live while
 * Phase A ingest runs (§5.1).
 */
export function useLibraryEvents(enabled: boolean): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) return;
    const token = authStore.getState().token;
    if (!token) return;

    const url = `${API_BASE_URL}/api/books/events?token=${encodeURIComponent(token)}`;
    const source = new EventSource(url);

    const onIngest = (event: MessageEvent<string>) => {
      try {
        const progress = parseIngestProgress(JSON.parse(event.data));
        if (!progress) return;

        queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (books) =>
          applyIngestProgress(books, progress),
        );

        if (isTerminalIngest(progress)) {
          void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
          void queryClient.invalidateQueries({ queryKey: queryKeys.book(progress.book_id) });
          void queryClient.invalidateQueries({ queryKey: queryKeys.shots(progress.book_id) });
        }
      } catch {
        /* malformed SSE frame — ignore */
      }
    };

    source.addEventListener("ingest_progress", onIngest as EventListener);
    return () => {
      source.removeEventListener("ingest_progress", onIngest as EventListener);
      source.close();
    };
  }, [enabled, queryClient]);
}
