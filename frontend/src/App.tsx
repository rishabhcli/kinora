function App() {
  return (
    <main className="flex min-h-full items-center justify-center bg-kinora-ink px-6 py-16">
      <section className="w-full max-w-xl rounded-3xl border border-white/10 bg-kinora-panel/80 p-10 shadow-2xl backdrop-blur">
        <p className="text-xs font-medium uppercase tracking-[0.35em] text-kinora-glow">
          watch the book
        </p>
        <h1 className="mt-4 text-5xl font-semibold tracking-tight text-kinora-mist">
          Kinora
        </h1>
        <p className="mt-4 text-base leading-relaxed text-kinora-muted">
          Any book becomes a page-synced film that generates itself a few seconds
          ahead of wherever you&rsquo;re reading. The two-pane reading workspace
          ships in the frontend phase.
        </p>
        <div className="mt-8 inline-flex items-center gap-2 rounded-full border border-kinora-glow/40 bg-kinora-glow/10 px-4 py-2 text-sm text-kinora-mist">
          <span className="h-2 w-2 rounded-full bg-kinora-glow" aria-hidden="true" />
          <span>Phase 1 scaffold online</span>
        </div>
      </section>
    </main>
  );
}

export default App;
