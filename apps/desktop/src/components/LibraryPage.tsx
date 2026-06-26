import { useCallback, useEffect, useMemo, useState } from "react";
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
    <div className="pt-12 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-2 pt-4">My Library</h1>
      <p className="text-sm text-kinora-muted mb-5">
        {loading
          ? "Loading your shelf…"
          : `${books.length} book${books.length === 1 ? "" : "s"} in your collection${offline ? " · offline" : ""}`}
      </p>

      <UploadBook onUploadsChange={setUploads} onReady={() => void load()} />

      {/* Search + sort */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <div className="relative flex-1 min-w-[220px]">
          <span aria-hidden className="absolute left-3 top-1/2 -translate-y-1/2 text-kinora-muted">
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
            className="w-full rounded-full pl-9 pr-3 py-2 text-[12px] text-kinora-text outline-none"
            style={{ background: "rgba(255,255,255,0.06)", border: "0.5px solid rgba(255,255,255,0.12)" }}
          />
        </div>
        <label className="flex items-center gap-2 text-[11px] text-kinora-muted">
          Sort
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortKey)}
            aria-label="Sort books"
            className="rounded-full px-3 py-1.5 text-[11px] text-kinora-text outline-none"
            style={{ background: "rgba(255,255,255,0.06)", border: "0.5px solid rgba(255,255,255,0.12)" }}
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
              className="rounded-full px-3 py-1.5 text-[11px] font-medium transition-colors"
              style={{
                background: active ? "rgba(212,164,78,0.9)" : "rgba(255,255,255,0.06)",
                color: active ? "#1a1408" : "rgba(232,226,216,0.82)",
                border: "0.5px solid rgba(255,255,255,0.12)",
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
        <div className="py-16 text-center text-sm text-kinora-muted">
          {query || genre !== "All"
            ? "No books match your search."
            : "Your library is empty — upload a book to begin."}
        </div>
      )}

      {shelves.map((s) => (
        <BookShelf key={s.title} title={s.title} books={s.books} onOpen={onOpenBook} />
      ))}
    </div>
  );
}
