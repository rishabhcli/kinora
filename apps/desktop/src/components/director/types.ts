// Shared types for the Director Studio component tree. The studio operates on a
// single book and an OPTIONAL live session: the §5.4 region-comment / re-roll /
// conflict tools all require a session id (they POST to /sessions/{id}/...),
// while the read-only canon vault + shot timeline only need the book id. The
// studio gracefully degrades when no session is open: it still shows the
// timeline + canon, and offers to start a session to enable the live tools.
import type { Book } from "../../data/books";

export interface StudioContext {
  book: Book;
  /** A live reading/director session, or null when none is open yet. The live
   *  tools (comment/regen, re-roll, conflict resolution) are gated on this. */
  sessionId: string | null;
  /** Create a session for this book so the live tools become available. Wired by
   *  the host (LibraryPage) against `api.createSession`. */
  onStartSession?: () => Promise<string>;
}

export type StudioTab = "timeline" | "canon" | "conflicts" | "annotations" | "analytics" | "share";
