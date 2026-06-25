import ContinueReadingCard from "./ContinueReadingCard";
import { currentlyReading } from "../data/books";

export default function HeroBanner() {
  return (
    <section className="relative w-full h-[480px] overflow-hidden">
      {/* Background image — LCP element, high priority */}
      <img
        src="/hero-bg.jpg"
        alt="The Midnight Library"
        className="absolute inset-0 w-full h-full object-cover"
        style={{ objectPosition: "center 20%" }}
        loading="eager"
        decoding="async"
        // @ts-expect-error fetchpriority is valid HTML but not in React types
        fetchpriority="high"
        width={1024}
        height={571}
      />

      {/* Gradient overlays — dark at bottom for readability, subtle at top */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            "linear-gradient(180deg, rgba(15,14,12,0.2) 0%, transparent 25%, rgba(15,14,12,0.4) 55%, rgba(15,14,12,0.95) 100%)",
        }}
      />
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            "linear-gradient(90deg, rgba(15,14,12,0.6) 0%, rgba(15,14,12,0.15) 40%, transparent 70%)",
        }}
      />

      {/* Content */}
      <div className="absolute inset-0 flex flex-col justify-end pt-16">
        <div className="max-w-[1280px] mx-auto w-full px-6 pb-8 relative">
          {/* Label */}
          <p className="text-[10px] font-medium text-kinora-muted uppercase tracking-widest mb-2">
            Featured Book
          </p>

          {/* Title */}
          <h1 className="font-serif text-4xl font-semibold text-kinora-text mb-3 max-w-lg leading-tight">
            The Midnight Library
          </h1>

          {/* Description */}
          <p className="text-[13px] text-kinora-muted max-w-md leading-relaxed mb-5">
            Between life and death there is a library. When Nora Seed finds
            herself in the Midnight Library, she has a chance to make things
            right. A dazzling novel about all the other lives we could have
            lived.
          </p>

          {/* Action buttons */}
          <div className="flex items-center gap-3 mb-4">
            <button
              aria-label="Read The Midnight Library now"
              className="flex items-center gap-2 px-5 py-2.5 rounded-lg text-[13px] font-medium transition-transform"
              style={{
                background: "rgba(232, 226, 216, 0.9)",
                color: "#0f0e0c",
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style={{ transition: "transform 0.2s ease" }} className="hover:scale-110 origin-center">
                <path d="M8 5v14l11-7z" />
              </svg>
              Read Now
            </button>
            <button
              aria-label="Watch cinematic adaptation of The Midnight Library"
              className="flex items-center gap-2 px-5 py-2.5 rounded-lg text-[13px] font-medium transition-colors"
              style={{
                background: "rgba(255, 255, 255, 0.06)",
                color: "rgba(232, 226, 216, 0.9)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="9" />
                <path d="M10 8.5l5 3.5-5 3.5z" fill="currentColor" stroke="none" />
              </svg>
              Watch Cinematic
            </button>
          </div>

          {/* Continue Reading card overlaid on banner */}
          <ContinueReadingCard book={currentlyReading} />
        </div>
      </div>
    </section>
  );
}
