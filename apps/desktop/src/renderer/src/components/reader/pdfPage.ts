/**
 * Shared types/helpers for the virtualised PDF reading pane (kinora.md §5.2).
 * The backend already serves, per page, a rasterised PNG (`image_url`) plus every
 * word's `[x, y, w, h]` box normalised to `[0, 1]` page coordinates (§9.4) — so
 * the overlay positions purely by percentage of the rendered image, and the
 * scroll-spy resolves the focus word from the same boxes.
 */
import { queryKeys } from "@kinora/core";

import { api } from "../../lib/api";

/** A page-load failure carrying the HTTP status, so the UI can tell a page that
 *  is still being extracted (404 during ingest) from a real fetch error. */
export class PageLoadError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "PageLoadError";
  }
}

/** Shared react-query options for one page's data — used by the row and by the
 *  column's prefetch so both hit the same cache entry. 404s aren't retried
 *  (the page is still being prepared); transient failures retry twice. */
export function pageQueryOptions(bookId: string, page: number) {
  return {
    queryKey: queryKeys.page(bookId, page),
    staleTime: 5 * 60 * 1000,
    retry: (failureCount: number, error: unknown) =>
      error instanceof PageLoadError && error.status === 404 ? false : failureCount < 2,
    queryFn: async () => {
      const { data, error, response } = await api.GET(
        "/api/books/{book_id}/pages/{page_number}",
        { params: { path: { book_id: bookId, page_number: page } } },
      );
      if (error || !data) throw new PageLoadError("failed to load page", response?.status);
      return data;
    },
  };
}

/** One word on a page: its book-global index, text, and normalised box. */
export interface WordBox {
  word_index: number;
  text: string;
  /** `[x, y, w, h]` in `[0, 1]` page coordinates. */
  bbox: [number, number, number, number];
}

/** Default page shape (width/height) used to reserve row space before the real
 *  image loads — a touch taller than wide, typical of a printed book page. */
export const DEFAULT_PAGE_RATIO = 0.66;

/** Coerce the loosely-typed `word_boxes` JSON into validated {@link WordBox}es. */
export function parseWordBoxes(
  raw: ReadonlyArray<Record<string, unknown>> | null | undefined,
): WordBox[] {
  if (!raw) return [];
  const out: WordBox[] = [];
  for (const row of raw) {
    const wordIndex = row["word_index"];
    const bbox = row["bbox"];
    if (typeof wordIndex !== "number" || !Array.isArray(bbox) || bbox.length < 4) continue;
    out.push({
      word_index: wordIndex,
      text: typeof row["text"] === "string" ? (row["text"] as string) : "",
      bbox: [Number(bbox[0]), Number(bbox[1]), Number(bbox[2]), Number(bbox[3])],
    });
  }
  return out;
}
