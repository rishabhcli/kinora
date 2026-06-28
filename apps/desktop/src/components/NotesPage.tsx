import { useEffect, useMemo, useState } from "react";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
  awardWinners,
  type Book,
} from "../data/books";

interface Note {
  id: number;
  book: string;
  text: string;
  date: string;
  page: number;
  tag: string;
}

const ALL_BOOKS: Book[] = [
  ...continueReading,
  ...recentlyAdded,
  ...popularOnKinora,
  ...recommended,
  ...awardWinners,
];

const bookById = new Map<string, Book>(ALL_BOOKS.map((b) => [b.id, b]));

function loadHighlightsFromStorage(): Note[] {
  const out: Note[] = [];
  let id = 1000;
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith("kinora.highlights.")) continue;
      const bookId = key.replace("kinora.highlights.", "");
      const book = bookById.get(bookId);
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      const arr = JSON.parse(raw) as Array<{ text: string; at: number }>;
      for (const hl of arr) {
        const date = new Date(hl.at);
        const now = Date.now();
        const diffMs = now - hl.at;
        const diffDays = Math.floor(diffMs / 86_400_000);
        let dateStr: string;
        if (diffDays < 1) dateStr = "Today";
        else if (diffDays === 1) dateStr = "Yesterday";
        else if (diffDays < 7) dateStr = `${diffDays} days ago`;
        else if (diffDays < 30) dateStr = `${Math.floor(diffDays / 7)} week${Math.floor(diffDays / 7) > 1 ? "s" : ""} ago`;
        else dateStr = date.toLocaleDateString();
        out.push({
          id: id++,
          book: book?.title ?? "Unknown",
          text: hl.text,
          date: dateStr,
          page: 0,
          tag: "Highlight",
        });
      }
    }
  } catch { /* storage blocked */ }
  return out;
}

export default function NotesPage() {
  const sampleNotes: Note[] = [
    { id: 1, book: "The Midnight Library", text: "Between life and death there is a library, and within that library, the shelves go on forever. Every book provides a chance to try another life you could have lived.", date: "2 days ago", page: 47, tag: "Highlight" },
    { id: 2, book: "Atomic Habits", text: "Small habits don't add up. They compound. That's the power of atomic habits — tiny changes, remarkable results.", date: "5 days ago", page: 23, tag: "Insight" },
    { id: 3, book: "Sapiens", text: "We did not domesticate wheat. It domesticated us. The Agricultural Revolution was history's biggest fraud.", date: "1 week ago", page: 89, tag: "Quote" },
    { id: 4, book: "The Midnight Library", text: "The only way to learn is to live. You can't learn from a life you didn't live. You can only learn from the one you did.", date: "1 week ago", page: 112, tag: "Reflection" },
    { id: 5, book: "Educated", text: "Everything I had worked for, all my years of study, had been to purchase for myself this one privilege: to see and experience more truths than those given to me by my father.", date: "2 weeks ago", page: 234, tag: "Highlight" },
    { id: 6, book: "The Psychology of Money", text: "Wealth is what you don't see. The car you didn't buy. The clothes you didn't buy. The diamond you didn't buy.", date: "3 weeks ago", page: 17, tag: "Insight" },
  ];

  const [savedHighlights, setSavedHighlights] = useState<Note[]>([]);

  useEffect(() => {
    setSavedHighlights(loadHighlightsFromStorage());
  }, []);

  const notes = useMemo(() => [...savedHighlights, ...sampleNotes], [savedHighlights]);

  const [search, setSearch] = useState("");
  const [activeTag, setActiveTag] = useState("All");
  const tags = ["All", "Highlight", "Insight", "Quote", "Reflection"];

  const filtered = notes.filter((n) => {
    const matchesSearch =
      n.text.toLowerCase().includes(search.toLowerCase()) ||
      n.book.toLowerCase().includes(search.toLowerCase());
    const matchesTag = activeTag === "All" || n.tag === activeTag;
    return matchesSearch && matchesTag;
  });

  return (
    <div className="pt-20 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      {/* Header — home page style with gold accent */}
      <div className="mb-6 pt-2">
        <div className="flex items-center gap-3 mb-2">
          <span className="inline-block" style={{ width: 28, height: 2, background: "linear-gradient(90deg, #d4a44e, transparent)" }} />
          <p className="text-[10px] font-semibold uppercase tracking-[0.26em]" style={{ color: "#d4a44e" }}>Annotations</p>
        </div>
        <h1 className="font-serif text-2xl font-semibold text-kinora-text">Notes</h1>
        <p className="text-[12px] text-kinora-muted mt-1.5">Highlights and reflections from your reading.</p>
      </div>

      <div className="flex items-center justify-between gap-3 mb-6">
        <div className="relative flex-1 max-w-xs">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search notes..."
            className="glass-input rounded-lg pl-8 pr-3 py-2 text-[12px] w-full"
          />
          <div className="absolute left-2.5 top-1/2 -translate-y-1/2 text-kinora-muted pointer-events-none">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="7" />
              <path d="M16.5 16.5L21 21" />
            </svg>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {tags.map((tag) => (
            <button
              key={tag}
              onClick={() => setActiveTag(tag)}
              className="px-3 py-1.5 rounded-full text-[11px] font-medium transition-all duration-200"
              style={{
                background: activeTag === tag
                  ? "linear-gradient(135deg, rgba(212,164,78,0.18) 0%, rgba(212,164,78,0.06) 100%)"
                  : "transparent",
                color: activeTag === tag ? "#e8c878" : "rgba(168, 158, 148, 0.7)",
                border: activeTag === tag ? "1px solid rgba(212,164,78,0.15)" : "1px solid transparent",
              }}
            >
              {tag}
            </button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="text-center py-16">
          <p className="text-kinora-muted text-sm">No notes found</p>
        </div>
      ) : (
        <div className="columns-1 md:columns-2 gap-4">
          {filtered.map((note) => (
            <div
              key={note.id}
              className="break-inside-avoid rounded-lg p-5 mb-4 transition-all duration-200"
              style={{
                background: "linear-gradient(180deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.015) 100%)",
                border: "1px solid rgba(255,255,255,0.07)",
                boxShadow: "0 4px 24px -12px rgba(0,0,0,0.3)",
              }}
            >
              <p className="text-[13px] text-kinora-text leading-relaxed mb-3">
                {note.text}
              </p>
              <div
                className="flex items-center justify-between gap-2 pt-2.5"
                style={{ borderTop: "1px solid rgba(255, 255, 255, 0.05)" }}
              >
                <div className="min-w-0">
                  <p className="text-[10px] text-kinora-muted truncate font-medium">
                    {note.book}
                  </p>
                  {note.page > 0 && <p className="text-[9px] text-kinora-muted/85">p. {note.page}</p>}
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <span className="text-[9px] text-kinora-muted/80">{note.tag}</span>
                  <span className="text-[9px] text-kinora-muted/85">{note.date}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
