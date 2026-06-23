import { type BookResponse, queryKeys } from "@kinora/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type ChangeEvent, type CSSProperties, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { BookCover } from "../components/BookCover";
import { DirectingStylePanel } from "../components/DirectingStylePanel";
import { ImportGateDialog } from "../components/ImportGateDialog";
import { MetricsPanel } from "../components/metrics/MetricsPanel";
import { SearchField } from "../components/SearchField";
import { useAuth } from "../hooks/useAuth";
import { useShelfIngestSync, shelfHasImporting } from "../hooks/useShelfIngestSync";
import { NATIVE_TOP_INSET, useNativeShell } from "../hooks/useNativeShell";
import { api } from "../lib/api";
import { authStore, persistToken } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

const PER_SHELF = 5;

async function uploadBook(file: File): Promise<boolean> {
  const form = new FormData();
  form.append("file", file);
  const token = authStore.getState().token;
  const response = await fetch(`${API_BASE_URL}/api/books`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form,
  });
  return response.ok;
}

/** A single oak shelf board with its lit top edge and shadowed front face. */
function Shelf() {
  return <div className="wood-rail mt-[-2px]" />;
}

/** A placeholder book standing on the plank while the library loads — a warm,
 *  shimmering spine the same size as a real cover, so opening the library reads
 *  as the shelf filling in rather than a blank wall. */
function CoverSkeleton({ delay = 0 }: { delay?: number }) {
  return (
    <div className="flex shrink-0 flex-col items-center" style={{ width: 138 }}>
      <div
        className="skeleton shimmer aspect-[2/3] w-[138px] rounded-[3px_7px_7px_3px]"
        style={{ "--shimmer-delay": `${delay}ms` } as CSSProperties}
      />
      <div className="shelf-contact mt-1 w-[86%] opacity-70" />
    </div>
  );
}

/** A ghost slot at the end of a sparse shelf that invites one more book — keeps
 *  a near-empty shelf feeling composed rather than abandoned. */
function AddSlot({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Add a book"
      className="no-drag group/add flex shrink-0 flex-col items-center outline-none"
      style={{ width: 138 }}
    >
      <div className="add-slot aspect-[2/3] w-[138px]">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
          <path d="M12 6v12M6 12h12" />
        </svg>
        <span className="text-[10px] font-medium uppercase tracking-[0.16em]">Add a book</span>
      </div>
      <div className="shelf-contact mt-1 w-[70%] opacity-40" />
    </button>
  );
}

export default function ShelfPage() {
  const email = useAuth((state) => state.user?.email);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const native = useNativeShell();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [query, setQuery] = useState("");
  const [showStyle, setShowStyle] = useState(false);
  // The book whose §13 metrics are open from the shelf (report-only — no live
  // session here, so the buffer sawtooth shows its "start reading" placeholder).
  const [metricsBookId, setMetricsBookId] = useState<string | null>(null);
  const [gateBook, setGateBook] = useState<BookResponse | null>(null);

  const { data: books, isLoading } = useQuery({
    queryKey: queryKeys.books(),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books");
      if (error || !data) throw new Error("failed to load books");
      return data;
    },
  });

  useShelfIngestSync(shelfHasImporting(books));

  // Warm each book's page-1 cover the moment the library resolves, so covers
  // appear instantly instead of streaming in one-by-one. We prefetch the same
  // query BookCover reads (filling the cache so BookCover renders from it), then
  // preload the resulting image into the browser cache via new Image().
  useEffect(() => {
    if (!books) return;
    let cancelled = false;
    for (const book of books) {
      if (book.status !== "ready") continue;
      void queryClient
        .fetchQuery({
          queryKey: queryKeys.page(book.id, 1),
          staleTime: 5 * 60 * 1000,
          queryFn: async () => {
            const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
              params: { path: { book_id: book.id, page_number: 1 } },
            });
            return error || !data ? null : data;
          },
        })
        .then((page) => {
          const url = page?.image_url;
          if (!cancelled && url) new Image().src = url;
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [books, queryClient]);

  const q = query.trim().toLowerCase();
  const filtered = (books ?? []).filter(
    (b) => !q || b.title.toLowerCase().includes(q) || (b.author ?? "").toLowerCase().includes(q),
  );
  const shelves: BookResponse[][] = [];
  for (let i = 0; i < filtered.length; i += PER_SHELF) shelves.push(filtered.slice(i, i + PER_SHELF));
  // Always show at least two boards so the room reads as a shelf with space to
  // grow — but not so many that a sparse library feels like a barren wall.
  while (shelves.length < 2) shelves.push([]);
  // The "add a book" ghost slot trails the last shelf that holds books (only
  // when there's room on it), so a sparse library still invites one more.
  const lastFilledShelf = Math.floor(Math.max(0, filtered.length - 1) / PER_SHELF);
  const showAddSlot = filtered.length > 0 && !q;
  // A sparse single shelf centers its books (and the add slot) so a lone book
  // doesn't drift to the top-left corner of a big empty wall.
  const sparse = filtered.length > 0 && filtered.length <= 2 && !q;

  function openBook(id: string) {
    const book = books?.find((b) => b.id === id);
    if (book && book.status !== "ready") {
      setGateBook(book);
      return;
    }
    const bridge = (globalThis as { kinora?: { openBook?: (bookId: string) => Promise<void> } }).kinora;
    if (bridge?.openBook) void bridge.openBook(id);
    else navigate(`/book/${id}`);
  }

  async function onFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setUploading(true);
    const ok = await uploadBook(file);
    setUploading(false);
    if (ok) void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
  }

  function signOut() {
    persistToken(null);
    authStore.getState().setAnonymous();
    navigate("/login");
  }

  const empty = !isLoading && filtered.length === 0;

  return (
    <div className="flex h-screen flex-col bg-transparent font-sans text-parchment">
      {/* Real Liquid Glass toolbar — this strip is transparent, so the window's
          native NSGlassEffectView shows through and frosts what's behind the window.
          Inside the native shell we sit below its glass title strip and drop the
          redundant "Library" wordmark (the native strip already shows branding);
          when native the left padding can relax since there's no traffic-light gutter. */}
      <header
        className={`drag relative z-30 flex shrink-0 items-center gap-3 border-b border-white/10 pr-5 ${
          native ? "pl-6" : "h-16 pl-24"
        }`}
        style={native ? { paddingTop: NATIVE_TOP_INSET, height: NATIVE_TOP_INSET + 64 } : undefined}
      >
        {!native && (
          <h1 className="font-display text-xl tracking-tight text-white [text-shadow:0_1px_8px_rgba(0,0,0,0.5)]">
            Library
          </h1>
        )}
        {email && <span className="hidden text-xs text-white/45 sm:inline">{email}</span>}
        <div className="no-drag ml-auto flex items-center gap-2">
          <SearchField value={query} onChange={setQuery} />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="flex h-9 items-center gap-1.5 rounded-full bg-white/[0.14] px-3.5 text-sm font-medium text-white backdrop-blur-md transition hover:bg-white/25 active:scale-[0.97] disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow focus-visible:ring-offset-2 focus-visible:ring-offset-transparent"
          >
            {uploading ? (
              <>
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-[1.5px] border-white/40 border-t-white motion-reduce:animate-none" />
                Adding…
              </>
            ) : (
              "Add book"
            )}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf,application/epub+zip,.epub,.pdf"
            className="hidden"
            onChange={onFile}
          />
          <div className="relative">
            <button
              onClick={() => setShowStyle((open) => !open)}
              title="Your directing style"
              aria-label="Your directing style"
              aria-expanded={showStyle}
              className={`flex h-9 w-9 items-center justify-center rounded-full backdrop-blur-md transition active:scale-[0.97] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow focus-visible:ring-offset-2 focus-visible:ring-offset-transparent ${
                showStyle
                  ? "bg-white/25 text-white"
                  : "bg-white/[0.14] text-white/80 hover:bg-white/25 hover:text-white"
              }`}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 8h18M7 8 5 4M12 8l-2-4M17 8l-2-4" />
                <rect x="3" y="8" width="18" height="12" rx="2" />
              </svg>
            </button>
            {showStyle && <DirectingStylePanel onClose={() => setShowStyle(false)} />}
          </div>
          <button
            onClick={signOut}
            title="Sign out"
            aria-label="Sign out"
            className="flex h-9 w-9 items-center justify-center rounded-full bg-white/[0.14] text-white/80 backdrop-blur-md transition hover:bg-white/25 hover:text-white active:scale-[0.97] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow focus-visible:ring-offset-2 focus-visible:ring-offset-transparent"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <path d="m16 17 5-5-5-5M21 12H9" />
            </svg>
          </button>
        </div>
      </header>

      {/* Opaque wooden wall + shelves (kept opaque so the desktop doesn't bleed through). */}
      <div className="relative flex-1 overflow-y-auto px-10 pb-20 pt-14">
        {/* Back wall: a warm walnut panel with a faint vertical grain. */}
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            backgroundColor: "#1d130c",
            backgroundImage:
              "linear-gradient(180deg, #2a1a10, #160d07 70%), repeating-linear-gradient(90deg, rgba(0,0,0,0.16) 0 1px, transparent 1px 9px)",
          }}
        />
        {/* Warm projector wash from above + a soft floor vignette so the shelves
            recede into the room. */}
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(100%_55%_at_50%_-8%,rgba(224,134,58,0.16),transparent_58%)]" />
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(130%_100%_at_50%_60%,transparent_38%,rgba(8,5,3,0.6))]" />
        <div className="relative mx-auto max-w-5xl">
          {/* Loading: a couple of shelves of shimmering placeholder spines so the
              library reads as filling in, not a blank wall. */}
          {isLoading &&
            [0, 1].map((row) => (
              <div key={row} className="mb-16">
                <div className="relative z-10 flex items-end gap-9 px-5" style={{ minHeight: 232 }}>
                  {Array.from({ length: 5 }).map((_, i) => (
                    <CoverSkeleton key={i} delay={(row * 5 + i) * 90} />
                  ))}
                </div>
                <Shelf />
              </div>
            ))}

          {/* Nothing matched the search — a quiet, distinct note (not the bare-shelf
              empty state). */}
          {!isLoading && empty && q && (
            <div className="mb-16">
              <div className="flex items-end justify-center" style={{ minHeight: 232 }}>
                <div className="glass max-w-sm rounded-glass px-7 py-6 text-center">
                  <p className="font-display text-lg text-white">No books match “{query.trim()}”</p>
                  <p className="mt-1 text-sm text-white/60">Try a different title or author.</p>
                </div>
              </div>
              <Shelf />
            </div>
          )}

          {/* A truly empty library — one composed, centered invitation. */}
          {!isLoading && empty && !q && (
            <div className="mb-16">
              <div className="flex items-end justify-center" style={{ minHeight: 232 }}>
                <div className="glass max-w-sm rounded-glass px-8 py-7 text-center">
                  <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-ember/15 text-ember-glow">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5Z" />
                      <path d="M13 4h5.5A1.5 1.5 0 0 1 20 5.5V14" opacity="0.55" />
                      <path d="M16.5 17.5v5M14 20h5" />
                    </svg>
                  </div>
                  <p className="font-display text-lg text-white">Your shelves are bare</p>
                  <p className="mt-1.5 text-sm text-white/60">
                    Add a PDF or EPUB and Kinora starts the film a few seconds ahead of your page.
                  </p>
                  <button
                    onClick={() => fileRef.current?.click()}
                    disabled={uploading}
                    className="mt-5 rounded-xl bg-gradient-to-b from-ember-glow to-ember-deep px-4 py-2 text-sm font-semibold text-walnut-deep shadow-[0_10px_28px_-10px_rgba(224,134,58,0.7)] transition hover:brightness-[1.06] active:scale-[0.99] disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow focus-visible:ring-offset-2 focus-visible:ring-offset-walnut-deep"
                  >
                    {uploading ? "Adding…" : "Add your first book"}
                  </button>
                </div>
              </div>
              <Shelf />
            </div>
          )}

          {!isLoading &&
            !empty &&
            shelves.map((row, i) => (
              <div key={i} className="mb-16">
                <div
                  className={`relative z-10 flex items-end gap-9 px-5 ${sparse ? "justify-center" : ""}`}
                  style={{ minHeight: 232 }}
                >
                  {row.map((book) => (
                    <BookCover
                      key={book.id}
                      book={book}
                      onOpen={() => openBook(book.id)}
                      onMetrics={() => setMetricsBookId(book.id)}
                    />
                  ))}
                  {showAddSlot && i === lastFilledShelf && row.length < PER_SHELF && (
                    <AddSlot onClick={() => fileRef.current?.click()} />
                  )}
                </div>
                <Shelf />
              </div>
            ))}
        </div>
      </div>

      {metricsBookId && (
        <MetricsPanel
          bookId={metricsBookId}
          sessionId={null}
          bookTitle={books?.find((b) => b.id === metricsBookId)?.title ?? null}
          onClose={() => setMetricsBookId(null)}
        />
      )}

      {gateBook && <ImportGateDialog book={gateBook} onClose={() => setGateBook(null)} />}
    </div>
  );
}
