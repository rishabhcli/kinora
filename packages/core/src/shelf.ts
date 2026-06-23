/**
 * Shelf helpers — import gating copy and React Query cache patches for live
 * ingest progress (§5.1). Shared by desktop and mobile so both shelves behave
 * the same when a book is still in Phase A.
 */
import type { BookResponse } from "./api/types";

/** Human label for an ingest stage slug (`shot_plan` → `Shot plan`). */
export function formatIngestStage(stage: string | null | undefined): string {
  const raw = stage?.trim();
  if (!raw) return "Preparing";
  if (raw === "ready") return "Ready";
  if (raw === "failed") return "Import failed";
  return raw.charAt(0).toUpperCase() + raw.slice(1).replace(/[_-]+/g, " ");
}

/** A short gate message when the reader tries to open a book that is not ready. */
export function importGateMessage(book: BookResponse): string | null {
  if (book.status === "ready") return null;
  if (book.status === "failed") {
    return "This import failed. Tap Remove on the cover, then try uploading again.";
  }
  const stage = formatIngestStage(book.stage);
  const pct =
    typeof book.progress === "number" && Number.isFinite(book.progress)
      ? Math.round(book.progress * 100)
      : null;
  if (pct != null) return `${stage} — ${pct}% complete`;
  return `${stage} — your film is being prepared`;
}

export interface IngestProgressPatch {
  book_id: string;
  stage?: string | null;
  pct?: number | null;
}

/** Merge a live ``ingest_progress`` event into a cached ``BookResponse``. */
export function applyIngestProgress(book: BookResponse, patch: IngestProgressPatch): BookResponse {
  if (book.id !== patch.book_id) return book;
  const stage = patch.stage ?? book.stage;
  const progress = patch.pct ?? book.progress;
  let status = book.status;
  if (stage === "ready") status = "ready";
  if (stage === "failed") status = "failed";
  return { ...book, stage: stage ?? book.stage, progress, status };
}

/** Patch a shelf list in place for one ingest event. */
export function patchBooksWithIngest(
  books: BookResponse[] | undefined,
  patch: IngestProgressPatch,
): BookResponse[] | undefined {
  if (!books?.length) return books;
  let changed = false;
  const next = books.map((book) => {
    const updated = applyIngestProgress(book, patch);
    if (updated !== book) changed = true;
    return updated;
  });
  return changed ? next : books;
}

/** Parse a Kinora upload error body into a user-facing string. */
export function uploadErrorMessage(body: unknown, fallback = "Upload failed"): string {
  if (body && typeof body === "object" && "error" in body) {
    const err = (body as { error?: { message?: string; type?: string } }).error;
    if (err?.message) return err.message;
    if (err?.type) return err.type.replace(/_/g, " ");
  }
  return fallback;
}
