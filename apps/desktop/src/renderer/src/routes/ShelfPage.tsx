import { queryKeys } from "@kinora/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type ChangeEvent, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../hooks/useAuth";
import { api } from "../lib/api";
import { authStore, persistToken } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

/** Upload a PDF as multipart/form-data (the typed client doesn't model binary bodies). */
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

export default function ShelfPage() {
  const email = useAuth((state) => state.user?.email);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);

  const {
    data: books,
    isLoading,
    isError,
  } = useQuery({
    queryKey: queryKeys.books(),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books");
      if (error || !data) throw new Error("failed to load books");
      return data;
    },
  });

  async function onFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setUploading(true);
    const ok = await uploadBook(file);
    setUploading(false);
    if (ok) void queryClient.invalidateQueries({ queryKey: queryKeys.books() });
  }

  function logout() {
    persistToken(null);
    authStore.getState().setAnonymous();
    navigate("/login");
  }

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-100">
      <header className="flex items-center justify-between border-b border-neutral-900 px-6 py-4">
        <h1 className="text-lg font-semibold tracking-tight">Your library</h1>
        <div className="flex items-center gap-3 text-sm text-neutral-400">
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="rounded-md bg-indigo-500 px-3 py-1 font-medium text-white hover:bg-indigo-400 disabled:opacity-50"
          >
            {uploading ? "Uploading…" : "Upload PDF"}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf"
            className="hidden"
            onChange={onFile}
          />
          {email && <span>{email}</span>}
          <button
            onClick={logout}
            className="rounded-md border border-neutral-800 px-3 py-1 hover:border-neutral-600"
          >
            Sign out
          </button>
        </div>
      </header>
      <main className="px-6 py-6">
        {isLoading && <p className="text-sm text-neutral-400">Loading your books…</p>}
        {isError && <p className="text-sm text-red-400">Couldn’t reach the backend.</p>}
        {books && books.length === 0 && (
          <p className="text-sm text-neutral-400">No books yet. Upload a PDF to begin.</p>
        )}
        {books && books.length > 0 && (
          <ul className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            {books.map((book) => (
              <li key={book.id}>
                <button
                  onClick={() => navigate(`/book/${book.id}`)}
                  className="w-full rounded-lg border border-neutral-800 bg-neutral-900 p-4 text-left transition-colors hover:border-neutral-600"
                >
                  <p className="truncate text-sm font-medium">{book.title}</p>
                  {book.author && (
                    <p className="truncate text-xs text-neutral-500">{book.author}</p>
                  )}
                  <p className="mt-2 text-xs uppercase tracking-wide text-neutral-500">
                    {book.status}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
