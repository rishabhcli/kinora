import { AnimatePresence, motion } from "framer-motion";
import { useState, useEffect, type CSSProperties } from "react";
import type { Book } from "../data/books";
import { api, toBrowserUrl } from "../lib/api";

interface BookReaderProps {
  book: Book | null;
  onClose: () => void;
}

// Easings: a soft settle for fades/scales, and a weighty hinge for the cover.
const SETTLE: [number, number, number, number] = [0.22, 1, 0.36, 1];
const HINGE: [number, number, number, number] = [0.66, 0, 0.2, 1];

export default function BookReader({ book, onClose }: BookReaderProps) {
  const [page, setPage] = useState(0);
  const [clipUrl, setClipUrl] = useState<string | null>(null);

  useEffect(() => {
    if (book) setPage(0);
  }, [book]);

  // For a real backend book, play its actual rendered shot clip. Mock catalogue
  // books 404 here and fall back to the bundled AI film.
  useEffect(() => {
    setClipUrl(null);
    if (!book || !api.isAuthed()) return;
    let alive = true;
    (async () => {
      try {
        const shots = await api.getShots(book.id);
        const withClip = shots.find((s) => s.clip_url);
        if (alive && withClip?.clip_url) setClipUrl(toBrowserUrl(withClip.clip_url));
      } catch {
        /* not a backend book / no clips yet — keep the bundled film */
      }
    })();
    return () => {
      alive = false;
    };
  }, [book]);

  // Lock body scroll while the reader is open.
  useEffect(() => {
    if (!book) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [book]);

  const pages = [
    "The first page felt heavy in her hands, as if the weight of every possible life pressed against her fingertips. Nora Seed stood at the threshold of the Midnight Library, a place between life and death where every book on the shelf was another life she could have lived.",
    "She pulled a book from the shelf. The title read: 'My Life as an Olympic Swimmer.' She opened it and felt the world shift around her, the library dissolving into the blue light of a swimming pool, the roar of a crowd filling her ears.",
    "Each book was a door. Each door led to a different version of herself. Some lives were bright, others were dim. Some were filled with love, others with regret. But all of them were hers — paths not taken, choices not made, words not spoken.",
    "As she moved through the shelves, Nora began to understand: the Midnight Library was not just about second chances. It was about seeing the value in the life she already had, the one she had been ready to leave behind.",
  ];

  // Each opened book plays one of the real generated adaptation films — Ken-Burns
  // scene clips produced by the backend render pipeline, bundled under /public.
  const FILMS = [
    "/generated/film-01.mp4",
    "/generated/film-02.mp4",
    "/generated/film-03.mp4",
    "/generated/film-04.mp4",
    "/generated/film-05.mp4",
    "/generated/film-06.mp4",
  ];
  const film = FILMS[(book ? [...book.id].reduce((a, c) => a + c.charCodeAt(0), 0) : 0) % FILMS.length];

  return (
    <AnimatePresence>
      {book && (
        <motion.div
          className="fixed inset-0 z-[100]"
          initial="closed"
          animate="open"
          exit="closed"
        >
          {/* 1 — Backdrop: the app behind blurs and dims as the book opens. */}
          <motion.div
            className="absolute inset-0"
            onClick={onClose}
            variants={{
              closed: { backdropFilter: "blur(0px)", backgroundColor: "rgba(8,7,6,0)" },
              open: { backdropFilter: "blur(20px)", backgroundColor: "rgba(8,7,6,0.74)" },
            }}
            transition={{ duration: 0.6, ease: SETTLE }}
          />

          {/* 2 — The reading experience that fills the screen, revealed from
                  behind the opening cover (scales up + sharpens into place). */}
          <motion.div
            className="absolute inset-0 flex flex-col kinora-bg"
            variants={{
              closed: { opacity: 0, scale: 1.06, filter: "blur(10px)" },
              open: { opacity: 1, scale: 1, filter: "blur(0px)" },
            }}
            transition={{
              opacity: { duration: 0.5, ease: SETTLE, delay: 0.42 },
              scale: { duration: 0.7, ease: SETTLE, delay: 0.42 },
              filter: { duration: 0.5, ease: SETTLE, delay: 0.42 },
            }}
            style={{ transformOrigin: "center" }}
          >
            {/* Top bar */}
            <div
              className="flex items-center gap-3 px-6 py-3 flex-shrink-0"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}
            >
              <button
                onClick={onClose}
                className="glass-control flex items-center gap-2 px-3 py-1.5 rounded-lg text-[12px] font-medium"
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

            {/* Reading content */}
            <div className="flex-1 overflow-y-auto">
              <div className="max-w-[900px] mx-auto px-6 py-8 flex gap-8">
                <div className="flex-shrink-0 w-[380px]">
                  {/* The generated film — real AI video (Wan), playing in-app. */}
                  <div className="glass-card rounded-xl overflow-hidden relative" style={{ aspectRatio: "16 / 9", boxShadow: "0 18px 50px rgba(0,0,0,0.55)" }}>
                    <video
                      key={clipUrl ?? film}
                      src={clipUrl ?? film}
                      poster={book.coverImage}
                      autoPlay
                      muted
                      loop
                      playsInline
                      controls
                      className="absolute inset-0 h-full w-full object-cover bg-black"
                    />
                    <div className="absolute left-2 top-2 flex items-center gap-1.5 rounded-full px-2 py-1" style={{ background: "rgba(0,0,0,0.45)", backdropFilter: "blur(8px)" }}>
                      <span className="inline-flex h-1.5 w-1.5 rounded-full" style={{ background: "#34d399", boxShadow: "0 0 6px #34d399" }} />
                      <span className="text-[9px] font-medium tracking-wide text-white/90">AI FILM</span>
                    </div>
                  </div>
                  <div className="mt-3 flex items-center gap-3">
                    <div className="relative flex-shrink-0 overflow-hidden rounded-md" style={{ width: 52, height: 78, background: book.coverGradient }}>
                      <img src={book.coverImage} alt="" className="absolute inset-0 h-full w-full object-cover" onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
                    </div>
                    <div className="min-w-0">
                      <p className="truncate font-serif text-[13px] leading-tight text-kinora-text">{book.title}</p>
                      <p className="truncate text-[11px] text-kinora-muted">{book.author}</p>
                      <p className="mt-1 text-[10px] text-kinora-muted/70">Generated with Wan · page-synced</p>
                    </div>
                  </div>
                </div>

                <div className="flex-1 min-w-0">
                  <p className="text-[10px] text-kinora-muted uppercase tracking-widest mb-2">Now Reading</p>
                  <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-1">{book.title}</h1>
                  <p className="text-[13px] text-kinora-muted mb-6">by {book.author}</p>

                  <div className="rounded-xl p-6 mb-4" style={{ background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.04)" }}>
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
                      className="glass-control flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium disabled:opacity-30"
                    >
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                        <path d="M15 18l-6-6 6-6" />
                      </svg>
                      Previous
                    </button>
                    <span className="text-[11px] text-kinora-muted">Page {page + 1} of {pages.length}</span>
                    <button
                      onClick={() => setPage((p) => Math.min(pages.length - 1, p + 1))}
                      disabled={page === pages.length - 1}
                      className="glass-control flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium disabled:opacity-30"
                    >
                      Next
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                        <path d="M9 18l6-6-6-6" />
                      </svg>
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </motion.div>

          {/* 3 — The cover that swings open on its spine, hinged on the left,
                  then lifts away. This is what reads as "opening a book". */}
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none" style={{ perspective: 2200 }}>
            <motion.div
              className="relative"
              style={{ width: "min(40vh, 300px)", aspectRatio: "2 / 3", transformStyle: "preserve-3d", transformOrigin: "left center" } as CSSProperties}
              variants={{
                closed: { rotateY: 0, opacity: 1 },
                open: { rotateY: -168, opacity: 0 },
              }}
              transition={{
                rotateY: { duration: 0.95, ease: HINGE, delay: 0.12 },
                opacity: { duration: 0.25, ease: "linear", delay: 0.95 },
              }}
            >
              {/* Front of the cover */}
              <div
                className="absolute inset-0 overflow-hidden"
                style={{
                  background: book.coverGradient,
                  borderRadius: "3px 8px 8px 3px",
                  backfaceVisibility: "hidden",
                  boxShadow: "0 30px 60px -20px rgba(0,0,0,0.85), 0 10px 24px -10px rgba(0,0,0,0.6)",
                }}
              >
                <img src={book.coverImage} alt="" className="absolute inset-0 w-full h-full object-cover" onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
                {/* spine shadow + gloss */}
                <div className="absolute inset-y-0 left-0" style={{ width: 14, background: "linear-gradient(90deg, rgba(0,0,0,0.4), transparent)" }} />
                <div className="absolute inset-0" style={{ background: "linear-gradient(105deg, rgba(255,255,255,0.18), transparent 36%, transparent 72%, rgba(0,0,0,0.28))" }} />
              </div>
              {/* Inside of the cover (seen as it swings past 90°) */}
              <div
                className="absolute inset-0"
                style={{
                  background: "linear-gradient(135deg, #28231e 0%, #15120e 100%)",
                  borderRadius: "8px 3px 3px 8px",
                  transform: "rotateY(180deg)",
                  backfaceVisibility: "hidden",
                  boxShadow: "inset 0 0 30px rgba(0,0,0,0.6)",
                }}
              />
            </motion.div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
