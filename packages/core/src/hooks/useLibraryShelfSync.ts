/**
 * Keeps the shelf's React Query books cache live while Phase A ingest runs:
 * SSE `ingest_progress` when EventSource is available, plus a 5s poll fallback
 * while any book is `importing`.
 */
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import type { TokenProvider } from "../api/client";
import type { BookResponse } from "../api/types";
import type { KinoraEvent } from "../events";
import { queryKeys } from "../query/keys";
import { applyIngestProgress } from "../shelf";
import {
  LibraryEventStream,
  type EventSourceFactory,
  type LibraryStreamStatus,
} from "../realtime/libraryStream";

const IMPORT_POLL_MS = 5_000;

export interface UseLibraryShelfSyncOptions {
  baseUrl: string;
  getToken: TokenProvider;
  queryClient: QueryClient;
  enabled: boolean;
  /** True when the cached shelf has at least one `importing` book — drives poll fallback. */
  hasImporting: boolean;
  /** When omitted (e.g. React Native), polling alone keeps the shelf fresh. */
  createEventSource?: EventSourceFactory;
  fetchBooks: () => Promise<BookResponse[]>;
  onStreamStatus?: (status: LibraryStreamStatus) => void;
}

export function useLibraryShelfSync({
  baseUrl,
  getToken,
  queryClient,
  enabled,
  hasImporting,
  createEventSource,
  fetchBooks,
  onStreamStatus,
}: UseLibraryShelfSyncOptions): void {
  const streamRef = useRef<LibraryEventStream | null>(null);
  const fetchRef = useRef(fetchBooks);
  fetchRef.current = fetchBooks;

  useEffect(() => {
    if (!enabled) {
      streamRef.current?.close();
      streamRef.current = null;
      return;
    }

    function handleEvent(event: KinoraEvent): void {
      if (event.event !== "ingest_progress") return;
      const payload = event as KinoraEvent & { book_id: string; stage?: string; pct?: number };
      queryClient.setQueryData<BookResponse[]>(queryKeys.books(), (books) =>
        applyIngestProgress(books, payload),
      );
      const pct = payload.pct;
      if (typeof pct === "number" && pct >= 1) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
      }
    }

    if (createEventSource) {
      const stream = new LibraryEventStream({
        baseUrl,
        getToken,
        createEventSource,
        onEvent: handleEvent,
        onStatus: onStreamStatus,
      });
      streamRef.current = stream;
      void stream.connect();
    }

    return () => {
      streamRef.current?.close();
      streamRef.current = null;
    };
  }, [baseUrl, createEventSource, enabled, getToken, onStreamStatus, queryClient]);

  const importing = enabled && hasImporting;

  useEffect(() => {
    if (!importing) return;
    let cancelled = false;

    async function poll(): Promise<void> {
      try {
        const fresh = await fetchRef.current();
        if (!cancelled) queryClient.setQueryData(queryKeys.books(), fresh);
      } catch {
        // The shelf query's own error UI handles hard failures.
      }
    }

    void poll();
    const timer = setInterval(() => void poll(), IMPORT_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [importing, queryClient]);
}
