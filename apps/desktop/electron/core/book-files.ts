/**
 * Book-file recognition — pure, Electron-free. Used by the protocol service to
 * decide whether an `open-file` / argv entry is a book Kinora should ingest.
 */
import path from "node:path";

const BOOK_EXTS = new Set([".pdf", ".epub"]);

export function isBook(filePath: string): boolean {
  return typeof filePath === "string" && BOOK_EXTS.has(path.extname(filePath).toLowerCase());
}

/** First book-extension path in an argv array, or null. */
export function findBookInArgv(argv: readonly string[]): string | null {
  for (const arg of argv) {
    if (typeof arg === "string" && isBook(arg)) return arg;
  }
  return null;
}
