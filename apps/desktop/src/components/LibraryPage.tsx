import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import type { Book } from "../data/books";
import { continueReading, popularOnKinora, recentlyAdded, recommended } from "../data/books";
import BookShelf from "./BookShelf";
import UploadBook, { type UploadItem } from "./UploadBook";
import {
  CATALOG_GENRES,
  listLibrary,
  searchBooks,
  shelvesFor,
  sortBooks,
  type LibraryBook,
  type SortKey,
} from "../lib/api/library";

// The Director Studio is a heavy, lazily-loaded overlay surface (the "second
// section" beside the reading room). It only mounts when the Director opens a
// book in studio mode, so the library's first paint stays light.
const DirectorStudio = lazy(() => import("./director/DirectorStudio"));
// The faceted "workbench" view is an alternate, power-user library surface
// (smart collections + facets + sorting); lazily loaded since it's opt-in.
const LibraryWorkbench = lazy(() => import("./library/LibraryWorkbench"));

type LibraryView = "shelves" | "workbench";

interface LibraryPageProps {
  /** Opens a book in the reading room — wired by the nav shell (Agent 10). */
  onOpenBook?: (book: Book) => void;
}

const SORTS: { key: SortKey; label: string }[] = [
  { key: "recent", label: "Recently added" },
  { key: "title", label: "Title A–Z" },
  { key: "author", label: "Author A–Z" },
  { key: "progress", label: "In progress" },
];

// Offline fallback: the curated mock shelves so the page never renders empty if
// the backend is unreachable (the real library replaces this once it loads).
const FALLBACK: Book[] = [...continueReading, ...recentlyAdded, ...popularOnKinora, ...recommended];

function optimisticBook(u: UploadItem): Book {
  return {
    id: `upload:${u.key}`,
    title: u.title,
    author: u.book?.author ?? "Importing…",
    progress: Math.round(u.progress),
    isNew: true,
    coverColor: "#2a2a2a",
    coverGradient: "linear-gradient(135deg, #3a3a3a 0%, #161616 100%)",
    coverImage: u.book?.coverImage ?? "",
    textColor: "#e8e2d8",
    spineColor: "#0a0a0a",
  };
}

export default function LibraryPage({ onOpenBook }: LibraryPageProps) {
  const [all, setAll] = useState<LibraryBook[] | null>(null);
  const [offline, setOffline] = useState(false);
  const [query, setQuery] = useState("");
  const [genre, setGenre] = useState("All");
  const [sort, setSort] = useState<SortKey>("recent");
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  // Director mode: when on, opening a book launches the Director Studio overlay
  // instead of the reading room. Only live (backend-driven) books can be
  // directed — a mock/offline book has no shots/canon to edit.
  const [directorMode, setDirectorMode] = useState(false);
  const [studioBook, setStudioBook] = useState<Book | null>(null);
  const [view, setView] = useState<LibraryView>("shelves");

  const load = useCallback(async () => {
    try {
      setAll(await listLibrary());
      setOffline(false);
    } catch {
      setAll(FALLBACK as LibraryBook[]);
      setOffline(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const books = all ?? [];

  // Open a book: in director mode, launch the studio (live books only); else
  // hand off to the reading-room flow owned by the nav shell.
  const handleOpen = useCallback(
    (book: Book) => {
      if (directorMode && book.live) {
        setStudioBook(book);
      } else {
        onOpenBook?.(book);
      }
    },
    [directorMode, onOpenBook],
  );

  const genresPresent = useMemo(
    () => CATALOG_GENRES.filter((g) => books.some((b) => b.genre === g)),
    [books],
  );
  const filtered = useMemo(() => {
    let result = searchBooks(books, query);
    if (genre !== "All") result = result.filter((b) => b.genre === genre);
    return sortBooks(result, sort);
  }, [books, query, genre, sort]);
  const shelves = useMemo(() => shelvesFor(filtered), [filtered]);

  const importing = uploads.filter((u) => u.status !== "ready").map(optimisticBook);
  const loading = all === null;

  return (
    <main id="kinora-main" className="pt-16 pb-12 px-6 max-w-[1280px] mx-auto relative z-10">
      {/* Header */}
      <div className="mb-8 pt-4">
        <p className="text-[11px] font-medium text-kinora-muted mb-2 tracking-wide uppercase">Collection</p>
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="font-serif text-3xl font-semibold text-kinora-text">My Library</h1>
            <p className="text-[13px] text-kinora-muted mt-2">
              {loading
                ? "Loading your shelf…"
                : `${books.length} book${books.length === 1 ? "" : "s"} in your collection${offline ? " · offline" : ""}`}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* View toggle — shelves (default) vs the faceted workbench. */}
            <div
              className="flex items-center rounded-xl p-0.5"
              style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.1)" }}
              role="group"
              aria-label="Library view"
            >
              {(["shelves", "workbench"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  aria-pressed={view === v}
                  className="rounded-lg px-3 py-1.5 text-[11px] font-medium capitalize transition-all"
                  style={{
                    background: view === v ? "rgba(212,164,78,0.18)" : "transparent",
                    color: view === v ? "rgba(236,231,223,0.98)" : "rgba(236,231,223,0.6)",
                  }}
                >
                  {v}
                </button>
              ))}
            </div>
            {/* Director-mode toggle — opening a live book then launches the Studio. */}
            <button
              type="button"
              onClick={() => setDirectorMode((v) => !v)}
              aria-pressed={directorMode}
              className="flex items-center gap-2 rounded-xl px-3.5 py-2 text-[11.5px] font-medium transition-all"
              style={{
                background: directorMode
                  ? "linear-gradient(135deg, #d4a44e 0%, #c8923a 100%)"
                  : "rgba(255,255,255,0.04)",
                color: directorMode ? "#1a1408" : "rgba(236,231,223,0.9)",
                border: `1px solid ${directorMode ? "rgba(212,164,78,0.4)" : "rgba(255,255,255,0.1)"}`,
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
                <rect x="2" y="6" width="14" height="12" rx="2" />
                <path d="m22 8-6 4 6 4V8z" />
              </svg>
              {directorMode ? "Director mode on" : "Director mode"}
            </button>
          </div>
        </div>
        {directorMode && (
          <p className="text-[11px] mt-2" style={{ color: "rgba(212,164,78,0.9)" }}>
            Open a live book to direct it — re-roll shots, comment to re-render, edit canon.
          </p>
        )}
      </div>

      <UploadBook onUploadsChange={setUploads} onReady={() => void load()} />

      {view === "workbench" ? (
        <Suspense fallback={<p className="text-[12px] text-kinora-muted py-8">Loading workbench…</p>}>
          <LibraryWorkbench books={books} onOpenBook={handleOpen} />
        </Suspense>
      ) : (
       <>
      {/* Search + sort */}
      <div className="flex flex-wrap items-center gap-3 mb-5">
        <div className="relative flex-1 min-w-[220px]">
          <span aria-hidden className="absolute left-3.5 top-1/2 -translate-y-1/2 text-kinora-muted">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m21 21-4.3-4.3" />
            </svg>
          </span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search title or author…"
            aria-label="Search your library by title or author"
            className="w-full rounded-xl pl-9 pr-3 py-2.5 text-[12.5px] text-kinora-text outline-none transition-all duration-200"
            style={{
              background: "linear-gradient(180deg, rgba(255,255,255,0.045) 0%, rgba(255,255,255,0.02) 100%)",
              border: "1px solid rgba(255,255,255,0.07)",
            }}
          />
        </div>
        <label className="flex items-center gap-2 text-[11px] text-kinora-muted">
          Sort
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortKey)}
            aria-label="Sort books"
            className="rounded-xl px-3.5 py-2 text-[11px] text-kinora-text outline-none transition-all duration-200"
            style={{
              background: "linear-gradient(180deg, rgba(255,255,255,0.045) 0%, rgba(255,255,255,0.02) 100%)",
              border: "1px solid rgba(255,255,255,0.07)",
            }}
          >
            {SORTS.map((s) => (
              <option key={s.key} value={s.key} style={{ color: "#1a1408" }}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {/* Genre filter chips */}
      <div className="flex flex-wrap gap-2 mb-8">
        {["All", ...genresPresent].map((c) => {
          const active = c === genre;
          return (
            <button
              key={c}
              onClick={() => setGenre(c)}
              aria-pressed={active}
              className="rounded-full px-3.5 py-1.5 text-[11px] font-medium transition-all duration-200"
              style={{
                background: active
                  ? "linear-gradient(135deg, #d4a44e 0%, #c8923a 100%)"
                  : "rgba(18,16,12,0.66)",
                color: active ? "#1a1408" : "rgba(236,231,223,0.96)",
                border: active
                  ? "1px solid rgba(212,164,78,0.3)"
                  : "1px solid rgba(255,255,255,0.08)",
                boxShadow: active ? "0 2px 12px -2px rgba(212,164,78,0.3)" : "none",
              }}
            >
              {c}
            </button>
          );
        })}
      </div>

      {importing.length > 0 && (
        <BookShelf title="Importing" books={importing} />
      )}

      {!loading && shelves.length === 0 && (
        <div
          className="py-20 text-center rounded-2xl"
          style={{
            background: "linear-gradient(180deg, rgba(255,255,255,0.025) 0%, rgba(255,255,255,0.01) 100%)",
            border: "1px solid rgba(255,255,255,0.05)",
          }}
        >
          <div className="mb-3" style={{ opacity: 0.3 }}>
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.2} strokeLinecap="round" strokeLinejoin="round" className="mx-auto text-kinora-muted">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
            </svg>
          </div>
          <p className="text-[13px] text-kinora-muted">
            {query || genre !== "All"
              ? "No books match your search."
              : "Your library is empty — upload a book to begin."}
          </p>
        </div>
      )}

      {shelves.map((s) => (
        <BookShelf key={s.title} title={s.title} books={s.books} onOpen={handleOpen} />
      ))}
       </>
      )}

      {studioBook && (
        <Suspense fallback={null}>
          <DirectorStudio
            book={studioBook}
            library={books}
            onClose={() => setStudioBook(null)}
          />
        </Suspense>
      )}
    </main>
  );
}
