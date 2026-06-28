// AnnotationHub — a book-wide browser for every annotation thread, with
// filtering by status (open/resolved) and tag, plus a jump-to-anchor action so
// a note can deep-link the Director to the shot it's about. Built on the same
// local AnnotationStore the per-shot ThreadPanel uses; this is the "all notes in
// one place" view (vs the inline per-shot list).
import { useEffect, useMemo, useState } from "react";
import {
  countThreads,
  sortThreads,
  type AnnotationStore,
  type AnnotationThread,
} from "../../lib/api/annotations";

interface AnnotationHubProps {
  bookId: string;
  annotations: AnnotationStore;
  /** Jump the studio to a thread's anchored shot (selects it on the timeline). */
  onJumpToShot?: (shotId: string) => void;
}

type StatusFilter = "all" | "open" | "resolved";

function useThreads(store: AnnotationStore, bookId: string): AnnotationThread[] {
  const [, setTick] = useState(0);
  useEffect(() => store.subscribe(() => setTick((n) => n + 1)), [store]);
  return store.forBook(bookId);
}

function anchorLabel(t: AnnotationThread): string {
  if (t.anchor.shot_id) return `Shot ${t.anchor.shot_id.slice(0, 6)}`;
  if (t.anchor.word_range) return `Words ${t.anchor.word_range[0]}–${t.anchor.word_range[1]}`;
  if (t.anchor.scene_id) return `Scene ${t.anchor.scene_id.slice(0, 6)}`;
  return "Book";
}

export default function AnnotationHub({ bookId, annotations, onJumpToShot }: AnnotationHubProps) {
  const threads = useThreads(annotations, bookId);
  const [status, setStatus] = useState<StatusFilter>("all");
  const [tag, setTag] = useState<string | null>(null);

  const allTags = useMemo(() => {
    const set = new Set<string>();
    for (const t of threads) t.tags.forEach((x) => set.add(x));
    return [...set].sort();
  }, [threads]);

  const filtered = useMemo(() => {
    let rows = threads;
    if (status === "open") rows = rows.filter((t) => !t.resolved);
    if (status === "resolved") rows = rows.filter((t) => t.resolved);
    if (tag) rows = rows.filter((t) => t.tags.includes(tag));
    return sortThreads(rows);
  }, [threads, status, tag]);

  const counts = countThreads(threads);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center rounded-lg p-0.5" style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.1)" }}>
          {(["all", "open", "resolved"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatus(s)}
              aria-pressed={status === s}
              className="rounded-md px-2.5 py-1 text-[10.5px] font-medium capitalize transition-all"
              style={{
                background: status === s ? "rgba(212,164,78,0.18)" : "transparent",
                color: status === s ? "rgba(236,231,223,0.98)" : "rgba(236,231,223,0.6)",
              }}
            >
              {s} {s === "all" ? counts.total : s === "open" ? counts.open : counts.resolved}
            </button>
          ))}
        </div>

        {allTags.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {allTags.map((x) => (
              <button
                key={x}
                type="button"
                onClick={() => setTag(tag === x ? null : x)}
                aria-pressed={tag === x}
                className="rounded-full px-2.5 py-1 text-[10px] font-medium transition-colors"
                style={{
                  background: tag === x ? "rgba(212,164,78,0.18)" : "rgba(255,255,255,0.04)",
                  color: "rgba(236,231,223,0.85)",
                  border: `1px solid ${tag === x ? "rgba(212,164,78,0.3)" : "rgba(255,255,255,0.08)"}`,
                }}
              >
                #{x}
              </button>
            ))}
          </div>
        )}
      </div>

      {filtered.length === 0 ? (
        <p className="text-[11px] text-kinora-muted py-6 text-center">
          {threads.length === 0 ? "No notes on this book yet." : "No notes match this filter."}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {filtered.map((t) => {
            const first = t.comments[0];
            return (
              <li
                key={t.id}
                className="rounded-xl p-3"
                style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.07)", opacity: t.resolved ? 0.7 : 1 }}
              >
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[10px] text-kinora-muted">{anchorLabel(t)} · {t.comments.length} msg</span>
                  <div className="flex items-center gap-2">
                    {t.resolved && <span className="text-[9px]" style={{ color: "#34d399" }}>resolved</span>}
                    {t.anchor.shot_id && onJumpToShot && (
                      <button
                        type="button"
                        onClick={() => onJumpToShot(t.anchor.shot_id!)}
                        className="text-[10px] font-medium text-kinora-muted hover:text-kinora-text transition-colors"
                      >
                        Jump to shot →
                      </button>
                    )}
                  </div>
                </div>
                {first && (
                  <p className="text-[11.5px] text-kinora-text/90">
                    <span className="font-medium text-kinora-text">{first.author}:</span> {first.body}
                  </p>
                )}
                {t.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {t.tags.map((x) => (
                      <span key={x} className="text-[9px] text-kinora-muted">#{x}</span>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
