/**
 * Upload a PDF/EPUB to the shelf with typed, user-facing error messages.
 */
import type { BookResponse } from "../api/types";

export type UploadBookResult =
  | { ok: true; book: BookResponse }
  | { ok: false; message: string };

type ApiErrorBody = {
  error?: {
    type?: string;
    message?: string;
    detail?: Record<string, unknown>;
  };
};

function messageForError(type: string | undefined, message: string | undefined): string {
  switch (type) {
    case "unsupported_media_type":
      return "That file type is not supported. Choose a PDF or EPUB.";
    case "book_quota_exceeded":
      return "Your library is full. Remove a book before adding another.";
    case "too_many_pages":
      return "That book has too many pages for Kinora to adapt.";
    case "invalid_pdf":
      return "The PDF could not be read. Try exporting it again.";
    case "invalid_epub":
      return "The EPUB could not be read. Try a different export.";
    default:
      return message?.trim() || "Upload failed. Try again.";
  }
}

/** POST /api/books with a bearer token and return a typed result. */
export async function uploadBook(
  apiBaseUrl: string,
  token: string | null,
  file: File,
  metadata?: { title?: string; author?: string; art_direction?: string },
): Promise<UploadBookResult> {
  const form = new FormData();
  form.append("file", file);
  if (metadata?.title) form.append("title", metadata.title);
  if (metadata?.author) form.append("author", metadata.author);
  if (metadata?.art_direction) form.append("art_direction", metadata.art_direction);

  const response = await fetch(`${apiBaseUrl.replace(/\/$/, "")}/api/books`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form,
  });

  if (response.ok) {
    const book = (await response.json()) as BookResponse;
    return { ok: true, book };
  }

  let body: ApiErrorBody | null = null;
  try {
    body = (await response.json()) as ApiErrorBody;
  } catch {
    body = null;
  }
  return {
    ok: false,
    message: messageForError(body?.error?.type, body?.error?.message),
  };
}
