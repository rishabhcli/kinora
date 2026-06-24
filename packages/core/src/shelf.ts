/**
 * Shelf helpers shared by desktop and mobile: human labels for import stages,
 * gating copy for books that are not ready yet, and cache updates for live
 * ingest progress (§5.1).
 */
import type { BookResponse } from "./api/types";

/** Strip internal seed suffixes and title-case all-lowercase upload names. */
export function displayBookTitle(title: string): string {
  const trimmed = title.replace(/\s*\(e2e seed\)\s*$/i, "").trim();
  if (!trimmed) return title;
  if (trimmed === trimmed.toLowerCase() && trimmed.includes(" ")) {
    return trimmed.replace(/\b\w/g, (ch) => ch.toUpperCase());
  }
  return trimmed;
}

/** A short, human label for a book that is not ready yet. */
export function stageLabel(book: Pick<BookResponse, "status" | "stage">): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

/** Normalised 0–1 ingest progress for display (SSE uses `pct`, API uses `progress`). */
export function ingestProgressFraction(
  book: Pick<BookResponse, "progress">,
  pct?: number | null,
): number | null {
  if (typeof pct === "number" && Number.isFinite(pct)) return Math.max(0, Math.min(1, pct));
  if (typeof book.progress === "number" && Number.isFinite(book.progress)) {
    return Math.max(0, Math.min(1, book.progress));
  }
  return null;
}

/** Whether any book on the shelf is still importing. */
export function hasImportingBooks(books: BookResponse[] | undefined): boolean {
  return (books ?? []).some((b) => b.status === "importing");
}

/** User-facing message when a non-ready book cannot be opened yet. */
export function importGateMessage(book: Pick<BookResponse, "status" | "stage" | "title">): string {
  if (book.status === "failed") {
    return `${displayBookTitle(book.title)} could not be adapted. Remove it and try uploading again.`;
  }
  const stage = stageLabel(book).toLowerCase();
  return `${displayBookTitle(book.title)} is still being adapted (${stage}). Check back on the shelf — progress updates live.`;
}

export interface IngestProgressPayload {
  book_id: string;
  stage?: string;
  pct?: number;
  progress?: number;
}

/** Merge a live ingest_progress event into the cached books list. */
export function applyIngestProgress(
  books: BookResponse[] | undefined,
  payload: IngestProgressPayload,
): BookResponse[] | undefined {
  if (!books) return books;
  const idx = books.findIndex((b) => b.id === payload.book_id);
  if (idx < 0) return books;
  const current = books[idx]!;
  const pct = payload.pct ?? payload.progress;
  const next: BookResponse = {
    ...current,
    status: "importing",
    stage: payload.stage ?? current.stage,
    progress: typeof pct === "number" ? pct : current.progress,
  };
  const copy = books.slice();
  copy[idx] = next;
  return copy;
}
