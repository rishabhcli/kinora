import { useState } from "react";
import type { Book } from "../data/books";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
} from "../data/books";
import BookShelf from "./BookShelf";

const SHELVES: { title: string; books: Book[] }[] = [
  { title: "Continue Reading", books: continueReading },
  { title: "Recently Added", books: recentlyAdded },
  { title: "Popular on Kinora", books: popularOnKinora },
  { title: "Recommended for You", books: recommended },
];

export default function LibraryPage() {
  const [filter, setFilter] = useState("All");
  const total = SHELVES.reduce((n, s) => n + s.books.length, 0);
  const chips = ["All", ...SHELVES.map((s) => s.title)];
  const shown = filter === "All" ? SHELVES : SHELVES.filter((s) => s.title === filter);

  return (
    <div className="pt-12 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-2 pt-4">My Library</h1>
      <p className="text-sm text-kinora-muted mb-5">{total} books in your collection</p>

      {/* Quick filters */}
      <div className="flex flex-wrap gap-2 mb-8">
        {chips.map((c) => {
          const active = c === filter;
          return (
            <button
              key={c}
              onClick={() => setFilter(c)}
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

      {shown.map((s) => (
        <BookShelf key={s.title} title={s.title} books={s.books} />
      ))}
    </div>
  );
}
