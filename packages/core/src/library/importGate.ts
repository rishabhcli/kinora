import type { BookResponse } from "../api/types";

/** User-facing copy when a book on the shelf is not yet openable. */
export function importGateMessage(book: BookResponse): { title: string; body: string } {
  if (book.status === "failed") {
    return {
      title: "Import failed",
      body: "Kinora could not finish preparing this book. Try uploading the PDF again with Add book, or confirm the file is a valid PDF or EPUB under 50 MB.",
    };
  }
  const stage = (book.stage ?? "preparing").replace(/[_-]+/g, " ");
  const pct =
    book.progress != null && book.status === "importing"
      ? Math.round(Math.min(1, Math.max(0, book.progress)) * 100)
      : null;
  return {
    title: "Still preparing",
    body:
      pct != null
        ? `This book is ${pct}% through ${stage}. It will open once Kinora finishes adapting it for the screen.`
        : `Kinora is still adapting this book (${stage}). Check back in a moment — the shelf updates live.`,
  };
}

/** Books that must not enter the reading room yet. */
export function bookIsOpenable(book: BookResponse | undefined | null): boolean {
  return book?.status === "ready";
}
