// Collaborative annotations + threads (Director domain) — reader/director notes
// anchored to a book region (a word-range and/or a shot), organized into
// resolvable threads. The backend has no annotation endpoint yet, so this is a
// local-first store with a stable, portable JSON wire format designed to sync to
// a future `/books/{id}/annotations` collection with no shape change (the
// `author`, `at`, `id` fields are server-authoritative-ready).
//
// Pure model + injectable KV persistence (mirrors lib/settings.ts), so threads,
// resolution, and the export/import round-trip are testable with no DOM.

// ---- Model ---------------------------------------------------------------- //

/** Where an annotation is anchored. At least one of {word_range, shot_id} is
 *  set; page is a convenience for rendering a margin marker. */
export interface AnnotationAnchor {
  word_range?: [number, number]; // [start, end] global word index
  shot_id?: string;
  scene_id?: string;
  page?: number;
}

/** A single message inside a thread. */
export interface AnnotationComment {
  id: string;
  author: string; // display name / user id; server-authoritative when synced
  body: string;
  at: number; // epoch ms
  /** Edited-at timestamp, if the body was changed after posting. */
  edited_at?: number;
}

/** A thread: an anchor + an ordered list of comments + resolution state. The
 *  first comment is the opening note; replies follow. */
export interface AnnotationThread {
  id: string;
  book_id: string;
  anchor: AnnotationAnchor;
  comments: AnnotationComment[];
  resolved: boolean;
  resolved_by?: string;
  resolved_at?: number;
  /** Free-form labels for filtering ("question", "continuity", "love-it"). */
  tags: string[];
  createdAt: number;
  updatedAt: number;
}

// ---- ID + clock seams (injectable for deterministic tests) ---------------- //

export interface Clock {
  now(): number;
}
export interface IdGen {
  next(prefix: string): string;
}

let _counter = 0;
const defaultIdGen: IdGen = {
  next: (prefix) => `${prefix}_${Date.now().toString(36)}_${(_counter++).toString(36)}`,
};
const systemClock: Clock = { now: () => Date.now() };

// ---- Pure thread operations ----------------------------------------------- //

export function anchorIsValid(anchor: AnnotationAnchor): boolean {
  if (anchor.word_range) {
    const [a, b] = anchor.word_range;
    if (!(Number.isFinite(a) && Number.isFinite(b) && a <= b)) return false;
  }
  return Boolean(anchor.word_range || anchor.shot_id || anchor.scene_id);
}

/** Sort threads for display: unresolved first, then most-recently-updated. */
export function sortThreads(threads: AnnotationThread[]): AnnotationThread[] {
  return [...threads].sort((a, b) => {
    if (a.resolved !== b.resolved) return a.resolved ? 1 : -1;
    return b.updatedAt - a.updatedAt;
  });
}

/** Threads anchored to a shot (the inspector's per-shot comment list). */
export function threadsForShot(threads: AnnotationThread[], shotId: string): AnnotationThread[] {
  return sortThreads(threads.filter((t) => t.anchor.shot_id === shotId));
}

/** Threads whose word-range overlaps [from, to] — the margin notes for a page
 *  span. Threads with no word anchor are excluded. */
export function threadsInWordRange(
  threads: AnnotationThread[],
  from: number,
  to: number,
): AnnotationThread[] {
  return sortThreads(
    threads.filter((t) => {
      const r = t.anchor.word_range;
      if (!r) return false;
      return r[0] <= to && r[1] >= from;
    }),
  );
}

export interface ThreadCounts {
  total: number;
  open: number;
  resolved: number;
}
export function countThreads(threads: AnnotationThread[]): ThreadCounts {
  const resolved = threads.filter((t) => t.resolved).length;
  return { total: threads.length, open: threads.length - resolved, resolved };
}

// ---- Persistence + store -------------------------------------------------- //

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const STORAGE_KEY = "kinora.annotations.v1";

function browserStore(): KeyValueStore | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    /* unavailable */
  }
  return null;
}

function isComment(v: unknown): v is AnnotationComment {
  if (typeof v !== "object" || v === null) return false;
  const r = v as Record<string, unknown>;
  return typeof r.id === "string" && typeof r.author === "string" && typeof r.body === "string" && typeof r.at === "number";
}

function parseThreads(raw: string | null): AnnotationThread[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: AnnotationThread[] = [];
  for (const row of parsed) {
    if (typeof row !== "object" || row === null) continue;
    const r = row as Record<string, unknown>;
    if (typeof r.id !== "string" || typeof r.book_id !== "string") continue;
    if (typeof r.anchor !== "object" || r.anchor === null) continue;
    const comments = Array.isArray(r.comments) ? r.comments.filter(isComment) : [];
    out.push({
      id: r.id,
      book_id: r.book_id,
      anchor: r.anchor as AnnotationAnchor,
      comments,
      resolved: r.resolved === true,
      resolved_by: typeof r.resolved_by === "string" ? r.resolved_by : undefined,
      resolved_at: typeof r.resolved_at === "number" ? r.resolved_at : undefined,
      tags: Array.isArray(r.tags) ? r.tags.filter((t): t is string => typeof t === "string") : [],
      createdAt: typeof r.createdAt === "number" ? r.createdAt : 0,
      updatedAt: typeof r.updatedAt === "number" ? r.updatedAt : 0,
    });
  }
  return out;
}

/** A portable, versioned export bundle — what "Share annotations" produces and
 *  "Import" consumes. Forward-compatible: an unknown `v` is rejected on import. */
export interface AnnotationExport {
  v: 1;
  book_id: string;
  exported_at: number;
  threads: AnnotationThread[];
}

export interface AnnotationStore {
  /** All threads for a book, display-sorted. */
  forBook(bookId: string): AnnotationThread[];
  /** Open a new thread with its first comment. */
  open(
    bookId: string,
    anchor: AnnotationAnchor,
    author: string,
    body: string,
    tags?: string[],
  ): AnnotationThread;
  /** Reply to an existing thread. Returns the updated thread, or null if absent. */
  reply(threadId: string, author: string, body: string): AnnotationThread | null;
  /** Edit a comment's body (provenance: stamps `edited_at`). */
  editComment(threadId: string, commentId: string, body: string): AnnotationThread | null;
  /** Toggle/set a thread's resolution. */
  setResolved(threadId: string, resolved: boolean, by?: string): AnnotationThread | null;
  /** Replace a thread's tag set. */
  setTags(threadId: string, tags: string[]): AnnotationThread | null;
  /** Delete a whole thread. */
  remove(threadId: string): void;
  /** Export a book's threads to a portable bundle. */
  exportBook(bookId: string): AnnotationExport;
  /** Merge an imported bundle into the store (new thread ids re-minted to avoid
   *  collisions). Returns how many threads were imported. */
  importBundle(bundle: unknown): number;
  subscribe(fn: () => void): () => void;
}

export function createAnnotationStore(
  backing?: KeyValueStore,
  deps: { clock?: Clock; ids?: IdGen } = {},
): AnnotationStore {
  const store = backing ?? browserStore();
  const clock = deps.clock ?? systemClock;
  const ids = deps.ids ?? defaultIdGen;
  let threads: AnnotationThread[] = parseThreads(store ? store.getItem(STORAGE_KEY) : null);
  const subs = new Set<() => void>();

  const persist = () => {
    try {
      store?.setItem(STORAGE_KEY, JSON.stringify(threads));
    } catch {
      /* write blocked */
    }
    subs.forEach((fn) => fn());
  };

  const find = (id: string) => threads.find((t) => t.id === id) ?? null;
  const update = (id: string, fn: (t: AnnotationThread) => AnnotationThread) => {
    const t = find(id);
    if (!t) return null;
    const next = fn(t);
    threads = threads.map((x) => (x.id === id ? next : x));
    persist();
    return next;
  };

  return {
    forBook: (bookId) => sortThreads(threads.filter((t) => t.book_id === bookId)),

    open(bookId, anchor, author, body, tags = []) {
      const now = clock.now();
      const thread: AnnotationThread = {
        id: ids.next("th"),
        book_id: bookId,
        anchor,
        comments: [{ id: ids.next("cm"), author, body, at: now }],
        resolved: false,
        tags,
        createdAt: now,
        updatedAt: now,
      };
      threads = [...threads, thread];
      persist();
      return thread;
    },

    reply: (threadId, author, body) =>
      update(threadId, (t) => ({
        ...t,
        comments: [...t.comments, { id: ids.next("cm"), author, body, at: clock.now() }],
        updatedAt: clock.now(),
      })),

    editComment: (threadId, commentId, body) =>
      update(threadId, (t) => ({
        ...t,
        comments: t.comments.map((c) =>
          c.id === commentId ? { ...c, body, edited_at: clock.now() } : c,
        ),
        updatedAt: clock.now(),
      })),

    setResolved: (threadId, resolved, by) =>
      update(threadId, (t) => ({
        ...t,
        resolved,
        resolved_by: resolved ? by : undefined,
        resolved_at: resolved ? clock.now() : undefined,
        updatedAt: clock.now(),
      })),

    setTags: (threadId, tags) =>
      update(threadId, (t) => ({ ...t, tags: [...tags], updatedAt: clock.now() })),

    remove(threadId) {
      const before = threads.length;
      threads = threads.filter((t) => t.id !== threadId);
      if (threads.length !== before) persist();
    },

    exportBook(bookId) {
      return {
        v: 1,
        book_id: bookId,
        exported_at: clock.now(),
        threads: threads.filter((t) => t.book_id === bookId),
      };
    },

    importBundle(bundle) {
      if (typeof bundle !== "object" || bundle === null) return 0;
      const b = bundle as Record<string, unknown>;
      if (b.v !== 1 || typeof b.book_id !== "string" || !Array.isArray(b.threads)) return 0;
      const incoming = parseThreads(JSON.stringify(b.threads));
      if (!incoming.length) return 0;
      // Re-mint thread ids so an import never clobbers an existing thread.
      const reIded = incoming.map((t) => ({ ...t, id: ids.next("th") }));
      threads = [...threads, ...reIded];
      persist();
      return reIded.length;
    },

    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}
