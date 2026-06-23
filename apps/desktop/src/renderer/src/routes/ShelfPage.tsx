import { type BookResponse, queryKeys } from "@kinora/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type ChangeEvent, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { BookCover } from "../components/BookCover";
import { SearchField } from "../components/SearchField";
import { useAuth } from "../hooks/useAuth";
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

export default function ShelfPage() {
  const email = useAuth((state) => state.user?.email);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [query, setQuery] = useState("");

  const { data: books, isLoading } = useQuery({
    queryKey: queryKeys.books(),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books");
      if (error || !data) throw new Error("failed to load books");
      return data;
    },
  });

  const q = query.trim().toLowerCase();
  const filtered = (books ?? []).filter(
    (b) => !q || b.title.toLowerCase().includes(q) || (b.author ?? "").toLowerCase().includes(q),
  );
  const shelves: BookResponse[][] = [];
  for (let i = 0; i < filtered.length; i += PER_SHELF) shelves.push(filtered.slice(i, i + PER_SHELF));
  while (shelves.length < 3) shelves.push([]);

  function openBook(id: string) {
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
          native NSGlassEffectView shows through and frosts what's behind the window. */}
      <header className="drag relative z-30 flex h-16 shrink-0 items-center gap-3 border-b border-white/10 pl-24 pr-5">
        <h1 className="font-display text-xl tracking-tight text-white [text-shadow:0_1px_8px_rgba(0,0,0,0.5)]">
          Library
        </h1>
        {email && <span className="hidden text-xs text-white/45 sm:inline">{email}</span>}
        <div className="no-drag ml-auto flex items-center gap-2">
          <SearchField value={query} onChange={setQuery} />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="flex h-9 items-center rounded-full bg-white/[0.14] px-3.5 text-sm font-medium text-white backdrop-blur-md transition hover:bg-white/25 disabled:opacity-60"
          >
            {uploading ? "Adding…" : "Add book"}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf,application/epub+zip,.epub,.pdf"
            className="hidden"
            onChange={onFile}
          />
          <button
            onClick={signOut}
            title="Sign out"
            className="flex h-9 w-9 items-center justify-center rounded-full bg-white/[0.14] text-white/80 backdrop-blur-md transition hover:bg-white/25"
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
          {isLoading && <p className="px-4 text-sm text-white/45">Opening your library…</p>}

          {empty && (
            <div className="mb-16">
              <div className="flex items-end justify-center" style={{ minHeight: 232 }}>
                <div className="glass max-w-sm rounded-glass p-7 text-center">
                  <p className="font-display text-lg text-white">Your shelves are bare</p>
                  <p className="mt-1 text-sm text-white/60">Add a PDF or EPUB and Kinora will start the film.</p>
                  <button
                    onClick={() => fileRef.current?.click()}
                    className="mt-4 rounded-xl bg-ember px-4 py-2 text-sm font-semibold text-walnut-deep transition hover:bg-ember-glow"
                  >
                    Add your first book
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
                <div className="relative z-10 flex items-end gap-9 px-5" style={{ minHeight: 232 }}>
                  {row.map((book) => (
                    <BookCover key={book.id} book={book} onOpen={() => openBook(book.id)} />
                  ))}
                </div>
                <Shelf />
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}
