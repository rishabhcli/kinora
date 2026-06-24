import type { BookResponse } from "./api/types";

/** Books still running Phase A ingest — the shelf should keep syncing these. */
export function booksNeedingSync(books: BookResponse[] | undefined): boolean {
  return (books ?? []).some((b) => b.status === "importing");
}

/** Merge a live ingest_progress payload into a shelf list (immutable). */
export function applyIngestProgress(
  books: BookResponse[] | undefined,
  bookId: string,
  patch: { stage?: string | null; progress?: number | null },
): BookResponse[] | undefined {
  if (!books) return books;
  let changed = false;
  const next = books.map((book) => {
    if (book.id !== bookId) return book;
    const stage = patch.stage ?? book.stage;
    const progress = patch.progress ?? book.progress;
    if (stage === book.stage && progress === book.progress) return book;
    changed = true;
    return { ...book, stage, progress };
  });
  return changed ? next : books;
}

/** Human title for shelf cards — strips internal seed suffixes from e2e fixtures. */
export function displayBookTitle(title: string): string {
  return title.replace(/\s*\(e2e seed\)\s*$/i, "").trim();
}

/** Sentence-case label for an import stage chip. */
export function stageLabel(book: BookResponse): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

/** User-facing copy when a book cannot be opened yet. */
export function importGateMessage(book: BookResponse): string {
  if (book.status === "failed") {
    return `${displayBookTitle(book.title)} could not be imported. Try uploading it again from desktop.`;
  }
  const label = stageLabel(book);
  const pct =
    typeof book.progress === "number" && Number.isFinite(book.progress)
      ? ` (${Math.round(book.progress * 100)}%)`
      : "";
  return `${displayBookTitle(book.title)} is still being prepared — ${label}${pct}. Check back in a moment.`;
}

/** Whether the reading room should open for this shelf book. */
export function canOpenBook(book: BookResponse): boolean {
  return book.status === "ready";
}

/** Map a failed `POST /api/books` response into shelf copy. */
export function uploadErrorMessage(status: number, body: unknown): string {
  const err = (body as { error?: { type?: string; message?: string } } | null)?.error;
  switch (err?.type) {
    case "file_too_large":
      return "That file is too large (max 50 MB).";
    case "invalid_pdf":
      return "That file isn't a readable PDF.";
    case "invalid_epub":
      return "That file isn't a readable EPUB.";
    case "too_many_pages":
      return "That book has too many pages (max 300).";
    case "book_quota_exceeded":
      return "Your library is full — remove a book before adding another.";
    case "unsupported_media_type":
      return "Kinora needs a PDF or EPUB file.";
    default:
      if (status === 401) return "Your session expired — sign in again.";
      if (status === 429) return "Too many uploads right now — wait a moment and try again.";
      return err?.message?.trim() || "Could not add that book. Try a different PDF or EPUB.";
  }
}
