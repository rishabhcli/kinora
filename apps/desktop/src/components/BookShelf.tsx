import BookCard from "./BookCard";
import type { Book } from "../data/books";

interface BookShelfProps {
  title: string;
  books: Book[];
  onOpen?: (book: Book) => void;
}

const ArrowIcon = ({ size = 16 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);

export default function BookShelf({ title, books, onOpen }: BookShelfProps) {
  return (
    <section className="mb-8 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-3 px-1">
        <div className="flex items-center gap-2">
          <div className="w-1 h-4 bg-kinora-gold/60" />
          <h2 className="font-serif text-base font-semibold text-kinora-text tracking-wide">
            {title}
          </h2>
        </div>
        <button aria-label={`See all ${title}`} className="flex items-center gap-1 text-[11px] text-kinora-muted hover:text-kinora-text transition-colors">
          <span>See All</span>
          <ArrowIcon size={10} />
        </button>
      </div>

      {/* Floating books row */}
      <div className="shelf-container relative">
        <div className="flex gap-4 overflow-x-auto hide-scrollbar px-1 pb-3">
          {books.map((book) => (
            <BookCard key={book.id} book={book} onOpen={onOpen} />
          ))}
        </div>
        <div className="shelf-shadow-line" />
      </div>
    </section>
  );
}
