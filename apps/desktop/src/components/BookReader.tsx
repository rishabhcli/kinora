import { AnimatePresence, motion } from "framer-motion";
import { useState, useEffect } from "react";
import type { Book } from "../data/books";

interface BookReaderProps {
  book: Book | null;
  onClose: () => void;
}

export default function BookReader({ book, onClose }: BookReaderProps) {
  const [page, setPage] = useState(0);

  useEffect(() => {
    if (book) setPage(0);
  }, [book]);

  const pages = [
    "The first page felt heavy in her hands, as if the weight of every possible life pressed against her fingertips. Nora Seed stood at the threshold of the Midnight Library, a place between life and death where every book on the shelf was another life she could have lived.",
    "She pulled a book from the shelf. The title read: 'My Life as an Olympic Swimmer.' She opened it and felt the world shift around her, the library dissolving into the blue light of a swimming pool, the roar of a crowd filling her ears.",
    "Each book was a door. Each door led to a different version of herself. Some lives were bright, others were dim. Some were filled with love, others with regret. But all of them were hers — paths not taken, choices not made, words not spoken.",
    "As she moved through the shelves, Nora began to understand: the Midnight Library was not just about second chances. It was about seeing the value in the life she already had, the one she had been ready to leave behind.",
  ];

  return (
    <AnimatePresence>
      {book && (
        <motion.div
          className="fixed inset-0 z-[100]"
          initial={{ opacity: 0, filter: "blur(12px)" }}
          animate={{ opacity: 1, filter: "blur(0px)" }}
          exit={{ opacity: 0, filter: "blur(12px)" }}
          transition={{ duration: 0.4, ease: [0.4, 0, 0.2, 1] }}
        >
          <div className="kinora-bg w-full h-full flex flex-col">
            {/* Top bar */}
            <div
              className="flex items-center gap-3 px-6 py-3 flex-shrink-0"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.05)" }}
            >
              <button
                onClick={onClose}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
                style={{ background: "rgba(255,255,255,0.04)", color: "rgba(232,226,216,0.9)" }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M15 18l-6-6 6-6" />
                </svg>
                Back
              </button>
              <div className="flex items-center gap-2 ml-2">
                <span className="font-serif text-sm font-semibold text-kinora-text">{book.title}</span>
                <span className="text-[11px] text-kinora-muted">· {book.author}</span>
              </div>
            </div>

            {/* Full reading page */}
            <motion.div
              className="flex-1 overflow-y-auto"
              initial={{ opacity: 0, scale: 1.04, filter: "blur(8px)" }}
              animate={{ opacity: 1, scale: 1, filter: "blur(0px)" }}
              transition={{ duration: 0.4, ease: [0.4, 0, 0.2, 1] }}
            >
              <div className="max-w-[900px] mx-auto px-6 py-8 flex gap-8">
                {/* Left: Book cover */}
                <div className="flex-shrink-0">
                  <div
                    className="rounded-lg overflow-hidden relative"
                    style={{
                      width: 240,
                      height: 360,
                      background: book.coverGradient,
                      boxShadow: "0 12px 40px rgba(0,0,0,0.5)",
                    }}
                  >
                    <img
                      src={book.coverImage}
                      alt={book.title}
                      className="absolute inset-0 w-full h-full object-cover"
                      onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                    />
                    <div
                      className="absolute inset-0"
                      style={{
                        background: "linear-gradient(135deg, rgba(255,255,255,0.1) 0%, transparent 30%, transparent 70%, rgba(255,255,255,0.03) 100%)",
                      }}
                    />
                    <div
                      className="absolute inset-y-0 left-0"
                      style={{ width: 8, background: "linear-gradient(90deg, rgba(0,0,0,0.3) 0%, transparent 100%)" }}
                    />
                  </div>
                </div>

                {/* Right: Reading content */}
                <div className="flex-1 min-w-0">
                  <p className="text-[10px] text-kinora-muted uppercase tracking-widest mb-2">Now Reading</p>
                  <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-1">{book.title}</h1>
                  <p className="text-[13px] text-kinora-muted mb-6">by {book.author}</p>

                  <div
                    className="rounded-xl p-6 mb-4"
                    style={{ background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.04)" }}
                  >
                    <AnimatePresence mode="wait">
                      <motion.p
                        key={page}
                        className="font-serif text-[14px] text-kinora-text/80 leading-relaxed"
                        initial={{ opacity: 0, x: 20 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: -20 }}
                        transition={{ duration: 0.25 }}
                      >
                        {pages[page]}
                      </motion.p>
                    </AnimatePresence>
                  </div>

                  <div className="flex items-center justify-between">
                    <button
                      onClick={() => setPage((p) => Math.max(0, p - 1))}
                      disabled={page === 0}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors disabled:opacity-30"
                      style={{ background: "rgba(255,255,255,0.04)", color: "rgba(232,226,216,0.9)" }}
                    >
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                        <path d="M15 18l-6-6 6-6" />
                      </svg>
                      Previous
                    </button>
                    <span className="text-[11px] text-kinora-muted">
                      Page {page + 1} of {pages.length}
                    </span>
                    <button
                      onClick={() => setPage((p) => Math.min(pages.length - 1, p + 1))}
                      disabled={page === pages.length - 1}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors disabled:opacity-30"
                      style={{ background: "rgba(255,255,255,0.04)", color: "rgba(232,226,216,0.9)" }}
                    >
                      Next
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                        <path d="M9 18l6-6-6-6" />
                      </svg>
                    </button>
                  </div>
                </div>
              </div>
            </motion.div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
