import type { BookResponse } from "@kinora/core";

function stageLabel(book: BookResponse): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Adapting your book";
}

/** Full-pane guard when a reader opens a book that is still importing. */
export function IngestProgressView({
  book,
  onBack,
}: {
  book: BookResponse;
  onBack: () => void;
}) {
  const pct = Math.round((book.progress ?? 0) * 100);
  const failed = book.status === "failed";

  return (
    <div className="flex h-screen flex-col items-center justify-center bg-walnut px-8 font-sans text-parchment">
      <div className="glass max-w-md rounded-glass px-8 py-8 text-center">
        <p className="font-display text-xl text-white">{book.title}</p>
        <p className="mt-2 text-sm text-white/65">
          {failed
            ? "Kinora could not finish adapting this book. Try uploading it again from the library."
            : "Kinora is turning this book into a page-synced film. This usually takes a few minutes."}
        </p>

        {!failed && (
          <div className="mt-6">
            <div className="mb-2 flex items-center justify-between text-xs uppercase tracking-[0.12em] text-white/55">
              <span>{stageLabel(book)}</span>
              <span>{pct}%</span>
            </div>
            <div className="ingest-progress-track">
              <div className="ingest-progress-fill" style={{ width: `${pct}%` }} />
            </div>
          </div>
        )}

        <button
          type="button"
          onClick={onBack}
          className="mt-7 rounded-xl bg-white/10 px-4 py-2 text-sm font-medium text-white transition hover:bg-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
        >
          Back to library
        </button>
      </div>
    </div>
  );
}
