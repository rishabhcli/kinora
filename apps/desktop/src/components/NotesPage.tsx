import { useState } from "react";

interface Note {
  id: number;
  book: string;
  text: string;
  date: string;
  page: number;
  tag: string;
}

export default function NotesPage() {
  const notes: Note[] = [
    { id: 1, book: "The Midnight Library", text: "Between life and death there is a library, and within that library, the shelves go on forever. Every book provides a chance to try another life you could have lived.", date: "2 days ago", page: 47, tag: "Highlight" },
    { id: 2, book: "Atomic Habits", text: "Small habits don't add up. They compound. That's the power of atomic habits — tiny changes, remarkable results.", date: "5 days ago", page: 23, tag: "Insight" },
    { id: 3, book: "Sapiens", text: "We did not domesticate wheat. It domesticated us. The Agricultural Revolution was history's biggest fraud.", date: "1 week ago", page: 89, tag: "Quote" },
    { id: 4, book: "The Midnight Library", text: "The only way to learn is to live. You can't learn from a life you didn't live. You can only learn from the one you did.", date: "1 week ago", page: 112, tag: "Reflection" },
    { id: 5, book: "Educated", text: "Everything I had worked for, all my years of study, had been to purchase for myself this one privilege: to see and experience more truths than those given to me by my father.", date: "2 weeks ago", page: 234, tag: "Highlight" },
    { id: 6, book: "The Psychology of Money", text: "Wealth is what you don't see. The car you didn't buy. The clothes you didn't buy. The diamond you didn't buy.", date: "3 weeks ago", page: 17, tag: "Insight" },
  ];

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
    <div className="pt-12 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-6 pt-4">
        Notes
      </h1>

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
              className="px-3 py-1.5 rounded-lg text-[11px] font-medium transition-colors"
              style={{
                background: activeTag === tag ? "rgba(255, 255, 255, 0.08)" : "transparent",
                color: activeTag === tag ? "rgba(232, 226, 216, 0.9)" : "rgba(168, 158, 148, 0.7)",
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
              className="break-inside-avoid rounded-xl p-4 mb-4"
              style={{
                background: "rgba(255, 255, 255, 0.025)",
                border: "1px solid rgba(255, 255, 255, 0.06)",
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
                  <p className="text-[9px] text-kinora-muted/85">p. {note.page}</p>
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
