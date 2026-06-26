import { useState } from "react";
import { continueReading, popularOnKinora } from "../data/books";

export default function WatchPage() {
  const watchable = [...continueReading.filter((b) => b.progress > 0), ...popularOnKinora];
  const [selected, setSelected] = useState(watchable[0]);
  const [isPlaying, setIsPlaying] = useState(false);

  return (
    <div className="pt-12 pb-8 max-w-[1280px] mx-auto relative z-10">
      {/* Full-bleed cinematic hero */}
      <div className="relative w-full overflow-hidden" style={{ aspectRatio: "21 / 9" }}>
        {/* Backdrop */}
        <div className="absolute inset-0">
          <img
            src={selected.coverImage}
            alt=""
            className="w-full h-full object-cover"
            onError={(e) => {
              (e.target as HTMLImageElement).style.display = "none";
            }}
          />
          <div className="absolute inset-0" style={{ background: selected.coverGradient }} />
        </div>

        {/* Gradient overlays — fade to dark at bottom */}
        <div className="absolute inset-0 bg-gradient-to-t from-[#0f0e0c] via-black/30 to-transparent" />
        <div className="absolute inset-0 bg-gradient-to-r from-black/50 via-transparent to-black/20" />

        {/* Content overlay */}
        <div className="absolute inset-0 flex flex-col justify-between p-8">
          {/* Top row — badges */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="text-[9px] font-bold px-2 py-0.5 rounded bg-white/10 text-white/80 border border-white/10 backdrop-blur-sm">
                FHD
              </span>
              <span className="text-[9px] font-medium px-2 py-0.5 rounded bg-white/5 text-white/70 border border-white/5 backdrop-blur-sm">
                AI CINEMATIC
              </span>
            </div>
            <span className="text-[10px] text-white/70 font-medium tracking-wide">
              {isPlaying ? "NOW PLAYING" : "READY"}
            </span>
          </div>

          {/* Bottom row — title + play */}
          <div className="flex items-end justify-between">
            <div className="max-w-[60%]">
              <p className="text-[10px] uppercase tracking-[0.25em] text-kinora-gold mb-2 font-medium">
                Kinora Cinematic
              </p>
              <h2 className="font-serif text-3xl font-semibold text-white mb-1 leading-tight">
                {selected.title}
              </h2>
              <p className="text-sm text-white/75">{selected.author}</p>
            </div>

            {/* Play / Pause button */}
            <button
              onClick={() => setIsPlaying(!isPlaying)}
              className="flex items-center gap-2 px-5 py-2.5 rounded-lg transition-all hover:scale-[1.03]"
              style={{
                background: "rgba(255,255,255,0.12)",
                backdropFilter: "blur(12px) saturate(160%)",
                WebkitBackdropFilter: "blur(12px) saturate(160%)",
                border: "1px solid rgba(255,255,255,0.15)",
                boxShadow: "0 4px 24px rgba(0,0,0,0.3), inset 0 1px 1px rgba(255,255,255,0.12)",
              }}
            >
              {isPlaying ? (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="white">
                    <rect x="7" y="5" width="3.5" height="14" rx="1" />
                    <rect x="13.5" y="5" width="3.5" height="14" rx="1" />
                  </svg>
                  <span className="text-[13px] font-semibold text-white">Pause</span>
                </>
              ) : (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="white">
                    <path d="M10 8.5l5 3.5-5 3.5z" />
                  </svg>
                  <span className="text-[13px] font-semibold text-white">Play</span>
                </>
              )}
            </button>
          </div>
        </div>

        {/* Bottom scrubber bar */}
        <div className="absolute bottom-0 left-0 right-0 h-1 bg-white/10">
          <div
            className="h-full bg-kinora-gold transition-all duration-300"
            style={{ width: isPlaying ? "35%" : "0%" }}
          />
        </div>
      </div>

      {/* Metadata strip below hero */}
      <div className="px-6 py-4 flex items-center justify-between border-b border-white/5 mb-6">
        <div className="flex items-center gap-4">
          {/* Mini poster */}
          <div
            className="w-8 h-12 rounded overflow-hidden flex-shrink-0"
            style={{ background: selected.coverGradient }}
          >
            <img
              src={selected.coverImage}
              alt=""
              className="w-full h-full object-cover"
              onError={(e) => {
                (e.target as HTMLImageElement).style.display = "none";
              }}
            />
          </div>
          <div>
            <p className="text-[11px] text-kinora-muted">
              {isPlaying ? "Resume from 12:34" : "Not started"}
            </p>
            <p className="text-[10px] text-kinora-subtle">
              ~45 min runtime
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button className="text-[11px] text-kinora-muted hover:text-kinora-text transition-colors flex items-center gap-1.5">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 20.5C12 20.5 3.5 15.5 3.5 9.5C3.5 6.5 5.8 4.5 8.5 4.5C10.2 4.5 11.5 5.5 12 6.5C12.5 5.5 13.8 4.5 15.5 4.5C18.2 4.5 20.5 6.5 20.5 9.5C20.5 15.5 12 20.5 12 20.5z" />
            </svg>
            Add to Favorites
          </button>
          <button className="text-[11px] text-kinora-muted hover:text-kinora-text transition-colors flex items-center gap-1.5">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 4.5C5 3.67 5.67 3 6.5 3H16l3 3v13.5c0 .83-.67 1.5-1.5 1.5h-11c-.83 0-1.5-.67-1.5-1.5z" />
              <path d="M16 3v3h3" />
              <path d="M8 10h8M8 13h8M8 16h5" strokeWidth={1.4} />
            </svg>
            View Notes
          </button>
        </div>
      </div>

      {/* Book selector */}
      <div className="px-6">
        <h2 className="font-serif text-base font-semibold text-kinora-text mb-3">
          Available to Watch
        </h2>
        <div className="flex gap-3 overflow-x-auto hide-scrollbar pb-3">
          {watchable.map((book) => {
            const isActive = book.id === selected.id;
            return (
              <button
                key={book.id}
                onClick={() => {
                  setSelected(book);
                  setIsPlaying(false);
                }}
                className={`flex-shrink-0 w-[140px] text-left transition-all duration-200 ${isActive ? "opacity-100" : "opacity-40 hover:opacity-70"}`}
              >
                <div
                  className={`relative rounded-md overflow-hidden mb-1.5 transition-all duration-200 ${isActive ? "ring-1 ring-kinora-gold/50" : ""}`}
                  style={{ aspectRatio: "2 / 3", background: book.coverGradient }}
                >
                  <img
                    src={book.coverImage}
                    alt={book.title}
                    className="absolute inset-0 w-full h-full object-cover"
                    loading="lazy"
                    onError={(e) => {
                      (e.target as HTMLImageElement).style.display = "none";
                    }}
                  />
                  <div className="absolute inset-0 book-spine" />
                  {isActive && (
                    <div className="absolute bottom-1 left-1 right-1 flex items-center justify-center">
                      <div
                        className="w-7 h-7 rounded-full flex items-center justify-center"
                        style={{
                          background: "rgba(0,0,0,0.6)",
                          backdropFilter: "blur(6px)",
                          WebkitBackdropFilter: "blur(6px)",
                        }}
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="white">
                          <path d="M10 8.5l5 3.5-5 3.5z" />
                        </svg>
                      </div>
                    </div>
                  )}
                </div>
                <h4 className="text-[11px] font-medium text-kinora-text truncate leading-tight">
                  {book.title}
                </h4>
                <p className="text-[10px] text-kinora-muted truncate">{book.author}</p>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
