import { INGEST_POLL_MS, queryKeys, shelfHasImporting } from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

/**
 * Poll the shelf while books import. React Native has no EventSource, so we
 * rely on short-interval refetches; desktop uses SSE + the same poll fallback.
 */
export function useShelfIngestSync(hasImporting: boolean): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!hasImporting) return undefined;
    void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
    const timer = setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
    }, INGEST_POLL_MS);
    return () => clearInterval(timer);
  }, [hasImporting, queryClient]);
}

export { shelfHasImporting };
