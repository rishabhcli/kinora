import { useRef, useCallback, useState, useEffect } from "react";
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

const LeftArrowIcon = ({ size = 16 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
    <path d="M19 12H5M11 6l-6 6 6 6" />
  </svg>
);

export default function BookShelf({ title, books, onOpen }: BookShelfProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(true);

  // Drag-to-scroll state
  const isDragging = useRef(false);
  const startX = useRef(0);
  const scrollStart = useRef(0);
  const dragDistance = useRef(0);

  const updateScrollButtons = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setCanScrollLeft(el.scrollLeft > 4);
    setCanScrollRight(el.scrollLeft < el.scrollWidth - el.clientWidth - 4);
  }, []);

  useEffect(() => {
    updateScrollButtons();
  }, [books, updateScrollButtons]);

  const scrollByAmount = (dir: "left" | "right") => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollBy({ left: dir === "left" ? -320 : 320, behavior: "smooth" });
  };

  const handleMouseDown = (e: React.MouseEvent) => {
    const el = scrollRef.current;
    if (!el) return;
    isDragging.current = true;
    startX.current = e.pageX;
    scrollStart.current = el.scrollLeft;
    dragDistance.current = 0;
    el.style.cursor = "grabbing";
    el.style.userSelect = "none";

    const onMove = (ev: MouseEvent) => {
      if (!isDragging.current) return;
      ev.preventDefault();
      const el2 = scrollRef.current;
      if (!el2) return;
      const walk = ev.pageX - startX.current;
      dragDistance.current = Math.abs(walk);
      el2.scrollLeft = scrollStart.current - walk;
    };

    const onUp = () => {
      isDragging.current = false;
      const el3 = scrollRef.current;
      if (el3) {
        el3.style.cursor = "grab";
        el3.style.userSelect = "";
      }
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const handleClickCapture = (e: React.MouseEvent) => {
    if (dragDistance.current > 5) {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  return (
    <section className="mb-8" style={{ contentVisibility: "auto", containIntrinsicSize: "1px 280px" }}>
      {/* Header */}
      <div className="flex items-center justify-between mb-3 px-1">
        <div className="flex items-center gap-2">
          <div className="w-1 h-4 bg-kinora-gold/60" />
          <h2 className="font-serif text-base font-semibold text-kinora-text tracking-wide">
            {title}
          </h2>
        </div>
        <div className="flex items-center gap-2">
          <button aria-label={`See all ${title}`} className="flex items-center gap-1 text-[11px] text-kinora-muted hover:text-kinora-text transition-colors">
            <span>See All</span>
            <ArrowIcon size={10} />
          </button>
        </div>
      </div>

      {/* Floating books row — drag to scroll */}
      <div className="shelf-container relative group/shelf">
        {/* Left arrow overlay */}
        {canScrollLeft && (
          <button
            aria-label={`Scroll ${title} left`}
            onClick={() => scrollByAmount("left")}
            className="absolute left-0 top-0 bottom-3 z-10 flex items-center justify-center w-8 transition-opacity"
            style={{
              background: "linear-gradient(90deg, rgba(15,14,12,0.8) 30%, transparent 100%)",
            }}
          >
            <LeftArrowIcon size={14} />
          </button>
        )}
        {/* Right arrow overlay */}
        {canScrollRight && (
          <button
            aria-label={`Scroll ${title} right`}
            onClick={() => scrollByAmount("right")}
            className="absolute right-0 top-0 bottom-3 z-10 flex items-center justify-center w-8 transition-opacity"
            style={{
              background: "linear-gradient(270deg, rgba(15,14,12,0.8) 30%, transparent 100%)",
            }}
          >
            <ArrowIcon size={14} />
          </button>
        )}
        <div
          ref={scrollRef}
          className="flex gap-4 overflow-x-auto hide-scrollbar px-1 pb-3 select-none"
          style={{ cursor: "grab" }}
          onMouseDown={handleMouseDown}
          onScroll={updateScrollButtons}
          onClickCapture={handleClickCapture}
        >
          {books.map((book) => (
            <BookCard key={book.id} book={book} onOpen={onOpen} />
          ))}
        </div>
        <div className="shelf-shadow-line" />
      </div>
    </section>
  );
}
