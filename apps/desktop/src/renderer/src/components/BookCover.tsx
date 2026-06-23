import { type BookResponse, queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../lib/api";

const SPINES = ["#3a2a4f", "#1f3a5f", "#3a1212", "#2b3b2e", "#4a3a2a", "#163b46"];
function colorFor(id: string): string {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return SPINES[h % SPINES.length] ?? SPINES[0]!;
}

/** A short, human label for a book that isn't ready yet — the import stage in
 *  sentence case, or a clean fallback. */
function stageLabel(book: BookResponse): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

/** A book standing on the shelf: its page-1 cover (or a titled spine box) sitting
 *  on the plank with a contact shadow, a tasteful hover lift, and a pop-out
 *  animation on select before it opens in its own window. A book still importing
 *  (or whose import failed) reads as a deliberate, dimmed state with a status
 *  chip rather than a broken cover. */
export function BookCover({ book, onOpen }: { book: BookResponse; onOpen: () => void }) {
  const [popping, setPopping] = useState(false);
  const ready = book.status === "ready";
  const failed = book.status === "failed";
  const working = !ready && !failed;

  const { data } = useQuery({
    queryKey: queryKeys.page(book.id, 1),
    enabled: ready,
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: book.id, page_number: 1 } },
      });
      return error || !data ? null : data;
    },
  });
  const cover = data?.image_url ?? null;

  function select() {
    setPopping(true);
    window.setTimeout(() => {
      onOpen();
      setPopping(false);
    }, 280);
  }

  return (
    <button
      onClick={select}
      title={book.title}
      aria-label={`Open ${book.title}`}
      className="group relative flex shrink-0 flex-col items-center outline-none"
      style={{ width: 138 }}
    >
      <div
        className={`relative aspect-[2/3] w-[138px] origin-bottom rounded-[3px_7px_7px_3px] transition-[transform,box-shadow] duration-[320ms] ease-[cubic-bezier(0.22,1,0.36,1)] will-change-transform group-hover:-translate-y-2.5 group-focus-visible:-translate-y-2.5 group-focus-visible:ring-2 group-focus-visible:ring-ember-glow/80 group-focus-visible:ring-offset-2 group-focus-visible:ring-offset-walnut-deep ${
          popping ? "-translate-y-8 scale-[1.08]" : ""
        }`}
        style={{
          boxShadow: popping
            ? "0 44px 64px -18px rgba(0,0,0,0.78), inset 0 0 0 1px rgba(255,255,255,0.08)"
            : "0 14px 26px -10px rgba(0,0,0,0.72), 0 2px 4px rgba(0,0,0,0.5)",
        }}
      >
        <div
          className={`relative h-full w-full overflow-hidden rounded-[3px_7px_7px_3px] transition-[filter,opacity] duration-300 ${
            ready ? "" : "opacity-90 saturate-[0.78] brightness-[0.72] group-hover:brightness-[0.82]"
          }`}
          style={cover ? undefined : { backgroundImage: `linear-gradient(150deg, ${colorFor(book.id)}, rgba(0,0,0,0.9))` }}
        >
          {cover ? (
            <img src={cover} alt={book.title} draggable={false} className="h-full w-full object-cover" />
          ) : (
            <div className="flex h-full flex-col justify-between p-3">
              <p className="line-clamp-4 font-display text-sm font-medium leading-tight text-white/95">
                {book.title}
              </p>
              {book.author && (
                <p className="line-clamp-1 text-[9px] uppercase tracking-[0.14em] text-white/55">
                  {book.author}
                </p>
              )}
            </div>
          )}

          {/* The bound spine edge (darkened left band) + a soft page sheen. */}
          <div className="pointer-events-none absolute inset-y-0 left-0 w-[7px] bg-gradient-to-r from-black/45 to-transparent" />
          <div className="pointer-events-none absolute inset-y-0 left-[7px] w-px bg-white/12" />
          <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(105deg,rgba(255,255,255,0.2),transparent_34%,transparent_88%,rgba(0,0,0,0.22))]" />

          {/* A book that's still importing or has failed: a soft scrim + a frosted
              status chip pinned to the foot of the cover, so it reads as a
              deliberate state rather than a broken card. */}
          {!ready && (
            <>
              <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-black/15" />
              {working && (
                <div className="shimmer pointer-events-none absolute inset-0 motion-reduce:hidden" />
              )}
              <div className="absolute inset-x-0 bottom-0 flex justify-center px-2 pb-2.5">
                <span className="status-chip" data-tone={failed ? "failed" : "working"}>
                  <span className="status-pulse" data-live={working ? "true" : undefined} />
                  {stageLabel(book)}
                </span>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Contact shadow on the plank: tightens + darkens as the book lifts. */}
      <div className="shelf-contact mt-1 w-[86%] opacity-90 group-hover:w-[78%] group-hover:opacity-60 group-focus-visible:w-[78%] group-focus-visible:opacity-60" />

      {/* Title sits just below the shelf board; absolute so the cover seats on
          the rail rather than the label. Fades in only on hover/focus. */}
      <p className="pointer-events-none absolute top-[calc(100%+12px)] left-1/2 max-w-[148px] -translate-x-1/2 truncate text-center font-sans text-[11px] text-white/0 transition-colors duration-200 group-hover:text-white/85 group-focus-visible:text-white/85">
        {book.title}
      </p>
    </button>
  );
}
