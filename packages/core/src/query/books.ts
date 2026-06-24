import type { BookResponse } from "../api/types";

/** How often the shelf polls while any owned book is still importing. */
export const BOOKS_POLL_INTERVAL_MS = 3_000;

/** True when the book is mid Phase-A ingest (the shelf should keep polling). */
export function isBookImporting(book: BookResponse): boolean {
  return book.status === "importing";
}

/** True when at least one book on the shelf is still importing. */
export function booksNeedPolling(books: BookResponse[] | undefined): boolean {
  return (books ?? []).some(isBookImporting);
}

/** A short, human label for an in-flight or failed import stage. */
export function ingestStageLabel(book: BookResponse): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

/** Import progress as an integer percent, or null when unknown. */
export function ingestProgressPercent(book: BookResponse): number | null {
  const pct = book.progress;
  if (pct == null || Number.isNaN(pct)) return null;
  return Math.max(0, Math.min(100, Math.round(pct * 100)));
}

/** Map a failed upload response to a user-facing message. */
export async function uploadErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as {
      error?: { type?: string; message?: string; detail?: Record<string, unknown> };
    };
    const err = body.error;
    if (!err) return fallbackUploadMessage(response.status);

    switch (err.type) {
      case "unsupported_media_type":
        return "That file isn't a PDF or EPUB. Choose a supported book format.";
      case "too_many_pages": {
        const max = err.detail?.max_pages;
        return typeof max === "number"
          ? `This book has too many pages (limit is ${max}). Try a shorter edition.`
          : "This book has too many pages for Kinora to adapt.";
      }
      case "book_quota_exceeded": {
        const max = err.detail?.max_books;
        return typeof max === "number"
          ? `You've reached the ${max}-book library limit. Remove a book before adding another.`
          : "Your library is full. Remove a book before adding another.";
      }
      case "validation_error":
        return "The upload didn't look like a valid book file. Check the format and try again.";
      default:
        return err.message?.trim() || fallbackUploadMessage(response.status);
    }
  } catch {
    return fallbackUploadMessage(response.status);
  }
}

function fallbackUploadMessage(status: number): string {
  if (status === 413) return "That file is too large. Try a smaller PDF or EPUB.";
  if (status === 415) return "That file isn't a PDF or EPUB.";
  if (status === 429) return "You've hit the upload limit. Try again later or remove a book.";
  if (status >= 500) return "The server couldn't accept that upload. Try again in a moment.";
  return "Upload failed. Check the file and try again.";
}
