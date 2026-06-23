import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../hooks/useAuth";
import { api } from "../lib/api";
import { authStore, persistToken } from "../lib/auth";

export default function ShelfPage() {
  const email = useAuth((state) => state.user?.email);
  const navigate = useNavigate();

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
