// ThreadPanel — collaborative annotation threads anchored to a shot (or a word
// range). Local-first via the injected AnnotationStore; designed to sync to a
// future backend annotations collection with no shape change. Renders the
// thread list for the anchor, an opener, inline replies, and resolve toggles.
import { useCallback, useEffect, useState } from "react";
import {
  threadsForShot,
  threadsInWordRange,
  countThreads,
  type AnnotationAnchor,
  type AnnotationStore,
  type AnnotationThread,
} from "../../lib/api/annotations";

interface ThreadPanelProps {
  bookId: string;
  anchor: AnnotationAnchor;
  annotations: AnnotationStore;
  author: string;
}

/** Subscribe a component to an annotation store and re-render on change. */
function useThreads(store: AnnotationStore, bookId: string): AnnotationThread[] {
  const [, setTick] = useState(0);
  useEffect(() => store.subscribe(() => setTick((n) => n + 1)), [store]);
  return store.forBook(bookId);
}

function relTime(ms: number): string {
  const diff = Date.now() - ms;
  const m = Math.round(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export default function ThreadPanel({ bookId, anchor, annotations, author }: ThreadPanelProps) {
  const allThreads = useThreads(annotations, bookId);
  const threads = anchor.shot_id
    ? threadsForShot(allThreads, anchor.shot_id)
    : anchor.word_range
      ? threadsInWordRange(allThreads, anchor.word_range[0], anchor.word_range[1])
      : [];
  const counts = countThreads(threads);

  const [draft, setDraft] = useState("");
  const [reply, setReply] = useState<Record<string, string>>({});

  const open = useCallback(() => {
    const body = draft.trim();
    if (!body) return;
    annotations.open(bookId, anchor, author, body);
    setDraft("");
  }, [draft, annotations, bookId, anchor, author]);

  return (
    <div className="rounded-xl p-3" style={{ background: "rgba(255,255,255,0.025)", border: "1px solid rgba(255,255,255,0.06)" }}>
      <div className="flex items-center justify-between mb-2">
        <p className="text-[11px] font-medium text-kinora-text">
          Notes {counts.total > 0 && <span className="text-kinora-muted">· {counts.open} open</span>}
        </p>
      </div>

      {/* Opener */}
      <div className="flex items-end gap-2 mb-3">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={1}
          placeholder="Add a note or question…"
          className="flex-1 resize-none rounded-lg px-2.5 py-1.5 text-[11.5px] text-kinora-text outline-none"
          style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)" }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              open();
            }
          }}
        />
        <button
          type="button"
          disabled={!draft.trim()}
          onClick={open}
          className="rounded-lg px-2.5 py-1.5 text-[10.5px] font-semibold transition-all disabled:opacity-40"
          style={{ background: "rgba(212,164,78,0.18)", color: "rgba(236,231,223,0.95)", border: "1px solid rgba(212,164,78,0.28)" }}
        >
          Post
        </button>
      </div>

      {threads.length === 0 ? (
        <p className="text-[10.5px] text-kinora-muted">No notes yet.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {threads.map((t) => (
            <li
              key={t.id}
              className="rounded-lg p-2.5"
              style={{
                background: t.resolved ? "rgba(255,255,255,0.015)" : "rgba(255,255,255,0.035)",
                border: "1px solid rgba(255,255,255,0.06)",
                opacity: t.resolved ? 0.65 : 1,
              }}
            >
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-[10px] text-kinora-muted">{t.comments.length} message{t.comments.length === 1 ? "" : "s"}</span>
                <button
                  type="button"
                  onClick={() => annotations.setResolved(t.id, !t.resolved, author)}
                  className="text-[10px] font-medium transition-colors"
                  style={{ color: t.resolved ? "#9aa3b2" : "#34d399" }}
                >
                  {t.resolved ? "Reopen" : "Resolve"}
                </button>
              </div>
              <ul className="flex flex-col gap-1.5">
                {t.comments.map((c) => (
                  <li key={c.id} className="text-[11px]">
                    <span className="font-medium text-kinora-text">{c.author}</span>{" "}
                    <span className="text-[9.5px] text-kinora-muted">{relTime(c.at)}{c.edited_at ? " · edited" : ""}</span>
                    <p className="text-kinora-text/90 whitespace-pre-wrap">{c.body}</p>
                  </li>
                ))}
              </ul>
              {!t.resolved && (
                <div className="flex items-end gap-2 mt-2">
                  <input
                    value={reply[t.id] ?? ""}
                    onChange={(e) => setReply((r) => ({ ...r, [t.id]: e.target.value }))}
                    placeholder="Reply…"
                    className="flex-1 rounded-lg px-2 py-1 text-[11px] text-kinora-text outline-none"
                    style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.07)" }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && (reply[t.id] ?? "").trim()) {
                        annotations.reply(t.id, author, reply[t.id].trim());
                        setReply((r) => ({ ...r, [t.id]: "" }));
                      }
                    }}
                  />
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
