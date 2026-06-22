import ReaderPreview from "./components/ReaderPreview";

const zones = [
  {
    name: "Committed",
    window: "0 – 45s ahead",
    body: "Full video, QA-passed and narrated. Cached and instantly playable — a re-read costs nothing.",
    dot: "bg-kinora-glow",
  },
  {
    name: "Speculative",
    window: "45 – 240s ahead",
    body: "One keyframe still per beat. Image-only, so guessing ahead of your eyes is nearly free.",
    dot: "ring-1 ring-inset ring-kinora-iris/70",
  },
  {
    name: "Cold",
    window: "240s+ ahead",
    body: "Plan and canon only. The text was analysed at import; nothing is rendered until you approach.",
    dot: "bg-kinora-line",
  },
];

function BrandMark({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="brand-bg" x1="4" y1="3" x2="28" y2="29" gradientUnits="userSpaceOnUse">
          <stop stopColor="#8b6dff" />
          <stop offset="1" stopColor="#4c1d95" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="28" height="28" rx="8" fill="url(#brand-bg)" />
      <path d="M6.5 9.5C6.5 8.4 7.4 7.6 8.5 7.8L11 8.3V24.2L8.5 24.7C7.4 24.9 6.5 24.1 6.5 23V9.5Z" fill="#ffffff" fillOpacity="0.3" />
      <path d="M13.2 11.1C13.2 10 14.4 9.3 15.4 9.8L22.8 13.9C23.8 14.5 23.8 15.9 22.8 16.5L15.4 20.6C14.4 21.1 13.2 20.4 13.2 19.3V11.1Z" fill="#ffffff" />
    </svg>
  );
}

function App() {
  return (
    <div id="top" className="relative flex min-h-full flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-kinora-glow focus:px-4 focus:py-2 focus:text-sm focus:font-semibold focus:text-white"
      >
        Skip to content
      </a>

      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 -z-10"
        style={{
          background:
            "radial-gradient(900px 600px at 12% -10%, rgba(124,92,255,0.18), transparent 60%), radial-gradient(720px 520px at 100% 110%, rgba(76,29,149,0.22), transparent 55%)",
        }}
      />

      <header className="sticky top-0 z-30 border-b border-kinora-line/60 bg-kinora-ink/70 backdrop-blur">
        <div className="mx-auto flex w-full max-w-6xl items-center justify-between gap-4 px-5 py-3 sm:px-8">
          <a href="#top" className="inline-flex items-center gap-2.5" aria-label="Kinora — home">
            <BrandMark className="h-7 w-7" />
            <span className="text-base font-semibold tracking-tight text-kinora-mist">Kinora</span>
          </a>
          <span className="inline-flex items-center gap-2 rounded-full border border-kinora-glow/40 bg-kinora-glow/10 px-3 py-1.5 text-xs font-medium text-kinora-mist">
            <span className="h-2 w-2 rounded-full bg-kinora-glow motion-safe:animate-pulse-glow" aria-hidden="true" />
            Phase 1 scaffold online
          </span>
        </div>
      </header>

      <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-5 sm:px-8">
        <section className="py-16 motion-safe:animate-fade-up sm:py-24" aria-labelledby="hero-heading">
          <p className="text-xs font-semibold uppercase tracking-[0.35em] text-kinora-iris">
            Watch the book
          </p>
          <h1
            id="hero-heading"
            className="mt-5 max-w-3xl text-balance text-4xl font-semibold leading-[1.05] tracking-tight text-kinora-mist sm:text-5xl lg:text-6xl"
          >
            Kinora turns any book into a <span className="text-kinora-iris">film</span> that plays as you read.
          </h1>
          <p className="mt-6 max-w-2xl text-base leading-relaxed text-kinora-muted sm:text-lg">
            A page-synced film generates itself a few seconds ahead of wherever you&rsquo;re reading. The
            words stay on screen, a narrator reads them aloud, and the page turns to follow the playhead —
            watch, read along, or both.
          </p>
          <div className="mt-9 flex flex-wrap items-center gap-3">
            <a
              href="#preview"
              className="inline-flex items-center gap-2 rounded-full bg-[#6d28d9] px-6 py-3 text-sm font-semibold text-white transition-colors hover:bg-[#7c5cff] focus-visible:ring-2 focus-visible:ring-kinora-iris focus-visible:ring-offset-2 focus-visible:ring-offset-kinora-ink"
            >
              See the preview
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
                <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.7-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14Z" />
              </svg>
            </a>
            <a
              href="#how"
              className="inline-flex items-center gap-2 rounded-full border border-kinora-line px-6 py-3 text-sm font-medium text-kinora-mist transition-colors hover:border-kinora-iris/60 hover:bg-white/5"
            >
              How it works
            </a>
          </div>
        </section>

        <section id="preview" className="scroll-mt-24 pb-16 sm:pb-24" aria-labelledby="preview-heading">
          <div className="mb-8 max-w-2xl">
            <h2 id="preview-heading" className="text-2xl font-semibold tracking-tight text-kinora-mist sm:text-3xl">
              A two-pane reading workspace
            </h2>
            <p className="mt-3 text-base leading-relaxed text-kinora-muted">
              The page on the left, the film on the right, kept in sync word by word. Press play, use the{" "}
              <kbd className="rounded border border-kinora-line bg-kinora-panel px-1.5 py-0.5 text-xs">←</kbd>{" "}
              <kbd className="rounded border border-kinora-line bg-kinora-panel px-1.5 py-0.5 text-xs">→</kbd>{" "}
              keys, or tap any word to scrub.
            </p>
          </div>
          <ReaderPreview />
          <p className="mt-4 text-sm text-kinora-muted/80">
            Fully on-device — this previews the reading experience with a public-domain excerpt. Live
            generation-on-scroll, narration, and the agent crew land in later phases.
          </p>
        </section>

        <section id="how" className="scroll-mt-24 border-t border-kinora-line/60 py-16 sm:py-24" aria-labelledby="how-heading">
          <div className="max-w-2xl">
            <h2 id="how-heading" className="text-2xl font-semibold tracking-tight text-kinora-mist sm:text-3xl">
              How generation-on-scroll works
            </h2>
            <p className="mt-3 text-base leading-relaxed text-kinora-muted">
              Reading is slow; video is short. A page of ~250 words takes a minute to read but maps to only
              a few seconds of film — so Kinora never renders the whole book. It renders the next few
              seconds, just ahead of your eyes.
            </p>
          </div>
          <ul className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {zones.map((zone) => (
              <li
                key={zone.name}
                className="rounded-2xl border border-kinora-line bg-kinora-panel/60 p-6 transition-colors hover:border-kinora-iris/40"
              >
                <div className="flex items-center gap-3">
                  <span className={`h-2.5 w-2.5 rounded-full ${zone.dot}`} aria-hidden="true" />
                  <h3 className="text-base font-semibold text-kinora-mist">{zone.name}</h3>
                </div>
                <p className="mt-1 text-xs font-medium uppercase tracking-[0.18em] text-kinora-iris/90">
                  {zone.window}
                </p>
                <p className="mt-3 text-sm leading-relaxed text-kinora-muted">{zone.body}</p>
              </li>
            ))}
          </ul>
        </section>
      </main>

      <footer className="border-t border-kinora-line/60">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-3 px-5 py-8 text-sm text-kinora-muted sm:flex-row sm:items-center sm:justify-between sm:px-8">
          <span className="inline-flex items-center gap-2.5">
            <BrandMark className="h-6 w-6" />
            <span className="font-medium text-kinora-mist">Kinora</span>
            <span aria-hidden="true">·</span>
            watch the book
          </span>
          <span className="text-kinora-muted/80">Phase 1 scaffold · frontend preview</span>
        </div>
      </footer>
    </div>
  );
}

export default App;
