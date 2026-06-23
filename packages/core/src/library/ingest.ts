/**
 * Live ingest progress helpers for the library shelf (§5.1).
 *
 * The backend publishes `ingest_progress` on `GET /api/books/events`; these
 * utilities parse those payloads and patch the cached book list in place.
 */
import type { BookResponse } from "../api/types";
import { parseSessionEvent } from "../events";

export type IngestProgressEvent = {
  event: "ingest_progress";
  book_id: string;
  stage?: string;
  pct?: number;
};

/** Parse a raw SSE payload into a typed ingest-progress event, or null. */
export function parseIngestProgress(raw: unknown): IngestProgressEvent | null {
  const event = parseSessionEvent(raw);
  if (event?.event !== "ingest_progress") return null;
  const payload = event as IngestProgressEvent & Record<string, unknown>;
  const stage = typeof payload.stage === "string" ? payload.stage : undefined;
  const pct = typeof payload.pct === "number" ? payload.pct : undefined;
  return { event: "ingest_progress", book_id: event.book_id, stage, pct };
}

/** Map an ingest stage to a shelf status when the backend omits it. */
export function statusFromIngestStage(stage: string | undefined): string | undefined {
  if (stage === "ready") return "ready";
  if (stage === "failed") return "failed";
  if (stage) return "importing";
  return undefined;
}

/** Whether this progress event marks the end of Phase A ingest. */
export function isTerminalIngest(event: IngestProgressEvent): boolean {
  return event.stage === "ready" || event.stage === "failed" || event.pct === 1;
}

/** Patch one book in a cached shelf list from a live ingest event. */
export function applyIngestProgress(
  books: BookResponse[] | undefined,
  event: IngestProgressEvent,
): BookResponse[] | undefined {
  if (!books) return books;
  const nextStatus = statusFromIngestStage(event.stage);
  return books.map((book) => {
    if (book.id !== event.book_id) return book;
    return {
      ...book,
      stage: event.stage ?? book.stage,
      progress: event.pct ?? book.progress,
      status: nextStatus ?? book.status,
    };
  });
}
