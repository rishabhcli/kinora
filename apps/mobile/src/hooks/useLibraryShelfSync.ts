import { hasImportingBooks, IMPORT_POLL_MS, queryKeys } from "@kinora/core";
import { type BookResponse } from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

/** Poll the shelf while any book is still importing (RN has no EventSource). */
export function useLibraryShelfSync(books: BookResponse[] | undefined, enabled: boolean): void {
  const queryClient = useQueryClient();
  const importing = enabled && books != null && hasImportingBooks(books);

  useEffect(() => {
    if (!importing) return;
    const timer = setInterval(() => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
    }, IMPORT_POLL_MS);
    return () => clearInterval(timer);
  }, [importing, queryClient]);
}
