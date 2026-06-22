import { Link } from "react-router-dom";

import type { Book } from "../../api/types";
import { coverGradient, initials } from "../../lib/cover";
import { useEventsStore } from "../../stores/eventsStore";

function progressPct(progress: number): number {
  const pct = progress <= 1 ? progress * 100 : progress;
  return Math.max(0, Math.min(100, Math.round(pct)));
}

function Cover({ book }: { book: Book }) {
  return (
    <div
      className="relative aspect-[2/3] w-full overflow-hidden rounded-2xl shadow-lg ring-1 ring-white/10"
      style={book.cover_url ? undefined : { background: coverGradient(book.title) }}
    >
      {book.cover_url ? (
        <img
          src={book.cover_url}
          alt=""
          className="h-full w-full object-cover"
          loading="lazy"
        />
      ) : (
        <div className="flex h-full w-full flex-col justify-between p-4">
          <span className="text-3xl font-semibold text-white/90">{initials(book.title)}</span>
          <span className="line-clamp-4 text-sm font-medium leading-snug text-white/85">
            {book.title}
          </span>
        </div>
      )}
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/30 to-transparent" />
    </div>
  );
}

function StatusLine({ book }: { book: Book }) {
  // Prefer a live ingest_progress event (kinora.md §5.1 / §5.6) when one has
  // arrived for this book; fall back to the polled book fields otherwise.
  const live = useEventsStore((s) => s.ingestProgress[book.id]);
  if (book.status === "importing") {
    const stage = live?.stage ?? book.stage ?? "preparing";
    const pct = progressPct(live?.pct ?? book.progress);
    return (
      <div className="mt-2">
        <div className="flex items-center justify-between text-[0.7rem] text-kinora-iris">
          <span>{stage}…</span>
          <span className="tabular-nums">{pct}%</span>
        </div>
        <div className="shimmer-track mt-1 h-1 w-full overflow-hidden rounded-full bg-kinora-line">
          <div
            className="h-full rounded-full bg-kinora-glow transition-[width] duration-300"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
    );
  }
  if (book.status === "failed") {
    return <p className="mt-2 text-xs text-kinora-danger">Import failed</p>;
  }
  return (
    <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-kinora-muted">
      <span className="h-1.5 w-1.5 rounded-full bg-kinora-ok" aria-hidden="true" />
      Ready · {book.num_pages || "—"} pages
    </p>
  );
}

export function BookCard({ book }: { book: Book }) {
  const inner = (
    <>
      <Cover book={book} />
      <h3 className="mt-3 line-clamp-1 text-sm font-semibold text-kinora-mist">{book.title}</h3>
      {book.author ? (
        <p className="line-clamp-1 text-xs text-kinora-muted">{book.author}</p>
      ) : null}
      <StatusLine book={book} />
    </>
  );

  if (book.status === "ready") {
    return (
      <Link
        to={`/book/${book.id}`}
        className="group block rounded-2xl outline-none transition-transform hover:-translate-y-1 focus-visible:-translate-y-1"
      >
        {inner}
      </Link>
    );
  }
  return (
    <div className={book.status === "importing" ? "block opacity-90" : "block opacity-70"}>
      {inner}
    </div>
  );
}
