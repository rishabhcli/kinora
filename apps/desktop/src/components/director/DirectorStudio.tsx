// DirectorStudio — the full-screen Director workspace for one book. It is the
// "second section" beside the reading room: a film-editing surface over the
// SAME backend (shots, canon, conflicts, prefs). Tabs:
//   • Timeline    — the scene/shot timeline + per-shot inspector (re-roll + §5.4
//                   region comments that RE-RENDER via the REST comment endpoint)
//   • Canon       — the editable canon vault (surgical §8.7 regen)
//   • Conflicts   — the §7.2 crew-dispute resolver
//   • Notes       — collaborative annotation threads (book-wide)
//   • Analytics   — reading-analytics + learned directing style
//   • Share       — sharing + export
//
// Live regen state: while a session is open, the studio subscribes to the
// session SSE stream and tracks which shots are re-rendering, swapping in the
// fresh clip on `regen_done`. With KINORA_LIVE_VIDEO OFF the loop still runs
// (Ken-Burns mp4s) so the UI is exercised end-to-end without spending credits.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Book } from "../../data/books";
import { api, type SessionEvent } from "../../lib/api";
import { director, type CanonGraph, type DirectorShot } from "../../lib/api/director";
import { annotationStore, analyticsStore } from "../../lib/api/stores";
import type { LibraryBook } from "../../lib/api/library";
import type { StudioTab } from "./types";
import SceneTimeline from "./SceneTimeline";
import ShotInspector from "./ShotInspector";
import CanonVault from "./CanonVault";
import ConflictPanel from "./ConflictPanel";
import AnnotationHub from "./AnnotationHub";
import AnalyticsDashboard from "./AnalyticsDashboard";
import ReadingHeatmap from "./ReadingHeatmap";
import SharePanel from "./SharePanel";

interface DirectorStudioProps {
  book: Book;
  /** The library shelf — feeds the analytics completion math. */
  library?: LibraryBook[];
  /** Display name for annotation authorship. */
  author?: string;
  onClose: () => void;
}

const TABS: { id: StudioTab; label: string }[] = [
  { id: "timeline", label: "Timeline" },
  { id: "canon", label: "Canon" },
  { id: "conflicts", label: "Conflicts" },
  { id: "annotations", label: "Notes" },
  { id: "analytics", label: "Analytics" },
  { id: "share", label: "Share" },
];

export default function DirectorStudio({ book, library = [], author = "You", onClose }: DirectorStudioProps) {
  const [tab, setTab] = useState<StudioTab>("timeline");
  const [shots, setShots] = useState<DirectorShot[]>([]);
  const [canon, setCanon] = useState<CanonGraph | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null);
  const [reRendering, setReRendering] = useState<Set<string>>(new Set());
  const [loadError, setLoadError] = useState<string | null>(null);

  // App-wide store singletons (shared with the reading room's analytics writes).
  const annotations = useMemo(() => annotationStore(), []);
  const analytics = useMemo(() => analyticsStore(), []);

  // Re-render note badges on the timeline; subscribe to the annotation store.
  const [annTick, setAnnTick] = useState(0);
  useEffect(() => annotations.subscribe(() => setAnnTick((n) => n + 1)), [annotations]);
  const noteCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const t of annotations.forBook(book.id)) {
      if (t.anchor.shot_id) counts[t.anchor.shot_id] = (counts[t.anchor.shot_id] ?? 0) + 1;
    }
    return counts;
    // annTick forces recompute when threads change
  }, [annotations, book.id, annTick]);

  const loadShots = useCallback(async () => {
    try {
      const rows = await director.getShots(book.id);
      setShots(rows);
      setSelectedShotId((cur) => cur ?? rows[0]?.shot_id ?? null);
    } catch {
      setLoadError("Couldn't load shots for this book.");
    }
  }, [book.id]);

  const loadCanon = useCallback(async () => {
    try {
      setCanon(await director.getCanon(book.id));
    } catch {
      /* canon may be empty for a freshly-uploaded book */
      setCanon({ book_id: book.id, entities: [], states: [], markdown: null });
    }
  }, [book.id]);

  useEffect(() => {
    void loadShots();
    void loadCanon();
  }, [loadShots, loadCanon]);

  // Open a session lazily so the live tools (comment/regen/conflict) work.
  const startSession = useCallback(async () => {
    if (sessionId) return sessionId;
    const s = await api.createSession(book.id, 0);
    setSessionId(s.session_id);
    return s.session_id;
  }, [book.id, sessionId]);

  // Subscribe to the session SSE stream for live regen state.
  const unsubRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    if (!sessionId) return;
    const handle = (e: SessionEvent) => {
      if (e.event === "agent_activity" && typeof (e as Record<string, unknown>).shot_id === "string") {
        const sid = (e as Record<string, unknown>).shot_id as string;
        setReRendering((prev) => new Set(prev).add(sid));
      } else if (e.event === "regen_done" || e.event === "clip_ready") {
        const sid = (e as Record<string, unknown>).shot_id as string | undefined;
        const oss = ((e as Record<string, unknown>).oss_url ?? (e as Record<string, unknown>).clip_url) as string | undefined;
        if (sid) {
          setReRendering((prev) => {
            const next = new Set(prev);
            next.delete(sid);
            return next;
          });
          if (oss) {
            setShots((prev) => prev.map((s) => (s.shot_id === sid ? { ...s, clip_url: oss, status: "accepted" } : s)));
          }
        }
      }
    };
    const unsub = api.openSessionEvents(sessionId, handle);
    unsubRef.current = unsub;
    return () => {
      unsub();
      unsubRef.current = null;
    };
  }, [sessionId]);

  const selectedShot = shots.find((s) => s.shot_id === selectedShotId) ?? null;

  // When a regen starts (re-roll or comment), mark the shot + ensure a session.
  const onRegenStarted = useCallback((shotId: string) => {
    setReRendering((prev) => new Set(prev).add(shotId));
  }, []);

  const onSelectShot = useCallback(
    (shot: DirectorShot) => {
      setSelectedShotId(shot.shot_id);
      // Lazily open a session the first time the Director engages a shot so the
      // live tools are ready without a session for read-only browsing.
      if (!sessionId) void startSession().catch(() => undefined);
    },
    [sessionId, startSession],
  );

  return (
    <div className="fixed inset-0 z-[60] flex flex-col" style={{ background: "rgba(12,11,9,0.97)" }}>
      {/* Top bar */}
      <header className="flex items-center justify-between px-5 py-3" style={{ borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
        <div className="flex items-center gap-3 min-w-0">
          <button
            type="button"
            onClick={onClose}
            aria-label="Close Director Studio"
            className="flex items-center gap-1.5 text-[12px] text-kinora-muted hover:text-kinora-text transition-colors"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <path d="M19 12H5M11 6l-6 6 6 6" />
            </svg>
            Back
          </button>
          <div className="min-w-0">
            <p className="text-[10px] uppercase tracking-wide text-kinora-muted">Director Studio</p>
            <h1 className="font-serif text-base font-semibold text-kinora-text truncate">{book.title}</h1>
          </div>
        </div>
        <div className="flex items-center gap-2 text-[10.5px]">
          {sessionId ? (
            <span className="rounded-full px-2.5 py-1" style={{ background: "rgba(52,211,153,0.14)", color: "#34d399", border: "1px solid rgba(52,211,153,0.28)" }}>
              Session live
            </span>
          ) : (
            <button
              type="button"
              onClick={() => void startSession()}
              className="rounded-full px-2.5 py-1 font-medium transition-colors"
              style={{ background: "rgba(212,164,78,0.16)", color: "rgba(236,231,223,0.95)", border: "1px solid rgba(212,164,78,0.28)" }}
            >
              Start session
            </button>
          )}
        </div>
      </header>

      {/* Tabs */}
      <nav className="flex items-center gap-1 px-5 py-2" style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }} role="tablist">
        {TABS.map((t) => {
          const active = t.id === tab;
          return (
            <button
              key={t.id}
              role="tab"
              aria-selected={active}
              onClick={() => setTab(t.id)}
              className="rounded-lg px-3 py-1.5 text-[11.5px] font-medium transition-all"
              style={{
                background: active ? "rgba(212,164,78,0.16)" : "transparent",
                color: active ? "rgba(236,231,223,0.98)" : "rgba(236,231,223,0.6)",
                border: `1px solid ${active ? "rgba(212,164,78,0.3)" : "transparent"}`,
              }}
            >
              {t.label}
            </button>
          );
        })}
      </nav>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {loadError && tab === "timeline" && (
          <p className="px-5 py-3 text-[11px]" style={{ color: "#f87171" }}>
            {loadError}
          </p>
        )}

        {tab === "timeline" && (
          <div className="flex flex-col lg:flex-row gap-5 px-5 py-5 max-w-[1280px] mx-auto">
            <div className="flex-1 min-w-0">
              <SceneTimeline
                shots={shots}
                selectedShotId={selectedShotId}
                onSelect={onSelectShot}
                reRendering={reRendering}
                noteCounts={noteCounts}
              />
            </div>
            <aside className="lg:w-[360px] shrink-0">
              {selectedShot ? (
                <ShotInspector
                  shot={selectedShot}
                  sessionId={sessionId}
                  bookId={book.id}
                  annotations={annotations}
                  author={author}
                  reRendering={reRendering.has(selectedShot.shot_id)}
                  onRegenStarted={onRegenStarted}
                />
              ) : (
                <p className="text-[12px] text-kinora-muted">Select a shot to inspect, re-roll, or direct it.</p>
              )}
            </aside>
          </div>
        )}

        {tab === "canon" && (
          <div className="px-5 py-5 max-w-[1280px] mx-auto">
            {canon ? (
              <CanonVault bookId={book.id} canon={canon} shots={shots} onEdited={() => void loadCanon()} />
            ) : (
              <p className="text-[12px] text-kinora-muted">Loading canon…</p>
            )}
          </div>
        )}

        {tab === "conflicts" && (
          <div className="px-5 py-5 max-w-[860px] mx-auto">
            <ConflictPanel sessionId={sessionId} />
          </div>
        )}

        {tab === "annotations" && (
          <div className="px-5 py-5 max-w-[860px] mx-auto">
            <AnnotationHub
              bookId={book.id}
              annotations={annotations}
              onJumpToShot={(shotId) => {
                setSelectedShotId(shotId);
                setTab("timeline");
              }}
            />
          </div>
        )}

        {tab === "analytics" && (
          <div className="px-5 py-5 max-w-[1080px] mx-auto flex flex-col gap-5">
            <AnalyticsDashboard books={library} analytics={analytics} bookId={book.id} />
            <ReadingHeatmap events={analytics.events()} />
          </div>
        )}

        {tab === "share" && (
          <div className="px-5 py-5 max-w-[720px] mx-auto">
            <SharePanel book={book} canon={canon} annotations={annotations} />
          </div>
        )}
      </div>
    </div>
  );
}
