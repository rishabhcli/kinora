import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, books as booksApi } from "../api/client";
import type { Book } from "../api/types";
import { Wordmark } from "../components/common/BrandMark";
import { BookGrid } from "../components/shelf/BookGrid";
import { SearchBar } from "../components/shelf/SearchBar";
import { UploadDropzone } from "../components/shelf/UploadDropzone";
import { useAuthStore } from "../stores/authStore";

function ShelfHeader() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  return (
    <header className="sticky top-0 z-30 border-b border-kinora-line/60 bg-kinora-ink/70 backdrop-blur">
      <div className="mx-auto flex w-full max-w-6xl items-center justify-between gap-4 px-5 py-3 sm:px-8">
        <Wordmark />
        <div className="flex items-center gap-3">
          {user ? (
            <span className="hidden text-sm text-kinora-muted sm:inline">{user.email}</span>
          ) : null}
          <button
            type="button"
            onClick={logout}
            className="rounded-full border border-kinora-line px-3 py-1.5 text-xs font-medium text-kinora-mist transition-colors hover:border-kinora-iris/60 hover:bg-white/5"
          >
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}

function CardSkeleton() {
  return (
    <div>
      <div className="aspect-[2/3] w-full animate-pulse rounded-2xl bg-kinora-panel" />
      <div className="mt-3 h-3 w-3/4 animate-pulse rounded bg-kinora-panel" />
    </div>
  );
}

export default function ShelfPage() {
  const [books, setBooks] = useState<Book[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const list = await booksApi.list(signal);
      setBooks(list);
      setError(null);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setError(err instanceof ApiError ? err.message : "Could not load your library.");
      setBooks((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    const ac = new AbortController();
    void refresh(ac.signal);
    return () => ac.abort();
  }, [refresh]);

  const hasImporting = (books ?? []).some((b) => b.status === "importing");
  useEffect(() => {
    if (!hasImporting) return undefined;
    const id = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(id);
  }, [hasImporting, refresh]);

  const onUploaded = (book: Book) => {
    setBooks((prev) => [book, ...(prev ?? []).filter((b) => b.id !== book.id)]);
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = books ?? [];
    if (!q) return list;
    return list.filter(
      (b) =>
        b.title.toLowerCase().includes(q) ||
        (b.author ? b.author.toLowerCase().includes(q) : false),
    );
  }, [books, query]);

  return (
    <div className="flex min-h-full flex-col">
      <ShelfHeader />
      <main className="mx-auto w-full max-w-6xl flex-1 px-5 py-8 sm:px-8">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-kinora-mist">Library</h1>
            <p className="mt-1 text-sm text-kinora-muted">
              Add a PDF to watch it become a page-synced film.
            </p>
          </div>
          <div className="w-full sm:max-w-xs">
            <SearchBar value={query} onChange={setQuery} />
          </div>
        </div>

        <div className="mt-6">
          <UploadDropzone onUploaded={onUploaded} />
        </div>

        {error ? (
          <p
            role="alert"
            className="mt-6 rounded-xl border border-kinora-danger/40 bg-kinora-danger/10 px-4 py-3 text-sm text-kinora-danger"
          >
            {error}
          </p>
        ) : null}

        <section className="mt-10">
          {books === null ? (
            <ul className="grid grid-cols-2 gap-x-5 gap-y-7 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
              {Array.from({ length: 5 }).map((_, i) => (
                <li key={i}>
                  <CardSkeleton />
                </li>
              ))}
            </ul>
          ) : filtered.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-kinora-line py-16 text-center">
              <p className="text-sm text-kinora-muted">
                {query
                  ? "No books match your search."
                  : "Your shelf is empty — upload a PDF to begin."}
              </p>
            </div>
          ) : (
            <BookGrid books={filtered} />
          )}
        </section>
      </main>
    </div>
  );
}
