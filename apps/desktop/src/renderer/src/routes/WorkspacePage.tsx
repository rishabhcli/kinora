import { bookIsOpenable, conflictResolution, importGateMessage, queryKeys, selectActiveConflict } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { sceneWindow, toDirectorShots } from "../components/director/shots";
import { MetricsPanel } from "../components/metrics/MetricsPanel";
import { CanonEditorPanel } from "../components/reader/canon/CanonEditorPanel";
import { AgentActivityFeed } from "../components/reader/AgentActivityFeed";
import { BufferIndicator } from "../components/reader/BufferIndicator";
import { CinemaPanel } from "../components/reader/CinemaPanel";
import { ConflictDialog } from "../components/reader/ConflictDialog";
import { PdfReadingColumn } from "../components/reader/PdfReadingColumn";
import { ReadingToolbar } from "../components/reader/ReadingToolbar";
import { useCanonRegen } from "../hooks/useCanonRegen";
import { useDirectorHistory } from "../hooks/useDirectorHistory";
import { useSyncEngine } from "../hooks/useSyncEngine";
import { api } from "../lib/api";
import { useReadingTheme } from "../lib/readingTheme";

const BOOKMARK_KEY = "kinora.bookmarks.v1";
const FEED_KEY = "kinora.feed.open.v1";
const MODE_KEY = "kinora.director.mode.v1";

type WorkspaceMode = "viewer" | "director";

function loadBookmarks(): Record<string, number> {
  if (typeof localStorage === "undefined") return {};
  try {
    return JSON.parse(localStorage.getItem(BOOKMARK_KEY) ?? "{}");
  } catch {
    return {};
  }
}

function loadFeedOpen(): boolean {
  return typeof localStorage !== "undefined" && localStorage.getItem(FEED_KEY) === "1";
}

/** The Viewer/Director mode the reader last left a given book in (§5.2/§5.4). */
function loadMode(bookId: string): WorkspaceMode | null {
  if (typeof localStorage === "undefined") return null;
  try {
    const map = JSON.parse(localStorage.getItem(MODE_KEY) ?? "{}");
    const m = (map as Record<string, unknown>)[bookId];
    return m === "viewer" || m === "director" ? m : null;
  } catch {
    return null;
  }
}

function saveMode(bookId: string, mode: WorkspaceMode): void {
  if (typeof localStorage === "undefined") return;
  try {
    const map = JSON.parse(localStorage.getItem(MODE_KEY) ?? "{}");
    map[bookId] = mode;
    localStorage.setItem(MODE_KEY, JSON.stringify(map));
  } catch {
    /* private mode — keep in memory */
  }
}

/** The reading room: a comfortable serif page (left) and the generated film
 *  (right) sharing one playhead, under an Apple Books-style glass toolbar. */
export default function WorkspacePage() {
  const { id: bookId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const reading = useReadingTheme();

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [shareConfirmed, setShareConfirmed] = useState(false);
  const [metricsOpen, setMetricsOpen] = useState(false);
  const [feedOpen, setFeedOpen] = useState(loadFeedOpen);
  const [canonOpen, setCanonOpen] = useState(false);
  // Tracks the surgical-regen lifecycle for the canon editor (§8.7): a canon edit
  // marks its dependent shots rendering, regen_done flips each to ready.
  const canonRegen = useCanonRegen();
  // The reader's per-shot directing history (§5.4) — tile badges + recent notes.
  const history = useDirectorHistory(bookId ?? null);
  // The top-of-view page the reading pane reports as it scrolls (drives the
  // page readout + bookmark).
  const [visiblePage, setVisiblePage] = useState(1);
  const [bookmarks, setBookmarks] = useState<Record<string, number>>(loadBookmarks);

  // ⌘/Ctrl+Shift+K toggles the canon editor (§5.4 power-user shortcut).
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setCanonOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!bookId) return;
    let cancelled = false;
    void api
      .POST("/api/sessions", { body: { book_id: bookId, focus_word: 0, mode: "viewer" } })
      .then(({ data }) => {
        if (!cancelled && data) setSessionId(data.session_id);
      });
    return () => {
      cancelled = true;
    };
  }, [bookId]);

  const {
    engine,
    snapshot,
    activity,
    budgetRemaining,
    socketStatus,
    bufferState,
    shotUpdates,
    markRegenerating,
    sendComment,
    resolveConflict,
  } = useSyncEngine(sessionId);

  // Conflicts the Director has closed out of the modal (the dispute stays in the
  // feed; this just stops re-opening it).
  const [dismissedConflicts, setDismissedConflicts] = useState<ReadonlySet<string>>(new Set());
  const dismissConflict = useCallback((conflictId: string) => {
    setDismissedConflicts((prev) => new Set(prev).add(conflictId));
  }, []);
  // The feed's "Resolve…" CTA un-dismisses a dispute so the dialog re-opens (§7.2).
  const reopenConflict = useCallback((conflictId: string) => {
    setDismissedConflicts((prev) => {
      if (!prev.has(conflictId)) return prev;
      const next = new Set(prev);
      next.delete(conflictId);
      return next;
    });
  }, []);

  const { data: book } = useQuery({
    queryKey: queryKeys.book(bookId ?? ""),
    enabled: Boolean(bookId),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}", {
        params: { path: { book_id: bookId as string } },
      });
      if (error || !data) throw new Error("failed to load book");
      return data;
    },
  });

  const { data: shots } = useQuery({
    queryKey: queryKeys.shots(bookId ?? ""),
    enabled: Boolean(bookId),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/shots", {
        params: { path: { book_id: bookId as string } },
      });
      if (error || !data) throw new Error("failed to load shots");
      return data;
    },
  });

  useEffect(() => {
    if (shots) engine.setShots(shots);
  }, [shots, engine]);

  // The page the playhead is on (playback / a seek); the reading pane follows it
  // while video owns the playhead.
  const enginePage = snapshot.currentPage || 1;

  // Feed the book's own page image into the ladder as the illustration rung
  // (§12.4) — the deep fallback the cinema stage pans when no keyframe exists for
  // a beat. react-query caches this; it only re-fetches as the page changes.
  const { data: currentPageData } = useQuery({
    queryKey: queryKeys.page(bookId ?? "", enginePage),
    enabled: Boolean(bookId) && enginePage > 0,
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: bookId as string, page_number: enginePage } },
      });
      if (error || !data) throw new Error("failed to load page");
      return data;
    },
  });
  useEffect(() => {
    if (currentPageData?.image_url) {
      engine.setPageIllustration(enginePage, currentPageData.image_url);
    }
  }, [currentPageData, enginePage, engine]);

  // The §7.2 dispute the Crew-dispute modal should show, its streamed resolution,
  // and the frame in question (the disputed shot's current clip).
  const activeConflict = useMemo(
    () => selectActiveConflict(activity, dismissedConflicts),
    [activity, dismissedConflicts],
  );
  const conflictTrace = useMemo(
    () => conflictResolution(activity, activeConflict),
    [activity, activeConflict],
  );
  const disputedClipUrl = useMemo(() => {
    if (!activeConflict?.shotId) return null;
    return shots?.find((s) => s.shot_id === activeConflict.shotId)?.clip_url ?? null;
  }, [shots, activeConflict]);

  // The §5.4 Director timeline: the book's shots merged with live clip/QA/regen
  // state, then windowed to the scene on screen.
  const directorShots = useMemo(
    () => toDirectorShots(shots ?? [], shotUpdates),
    [shots, shotUpdates],
  );
  const sceneShots = useMemo(
    () => sceneWindow(directorShots, snapshot.currentShotId),
    [directorShots, snapshot.currentShotId],
  );

  // Resume the Viewer/Director mode the reader left this book in (§5.2/§5.4), and
  // persist it as they switch — a Director session reopens where it left off.
  useEffect(() => {
    if (!bookId) return;
    const saved = loadMode(bookId);
    if (saved) engine.setMode(saved);
  }, [bookId, engine]);
  useEffect(() => {
    if (bookId) saveMode(bookId, snapshot.mode);
  }, [bookId, snapshot.mode]);

  const toggleFeed = () => {
    setFeedOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(FEED_KEY, next ? "1" : "0");
      } catch {
        /* private mode — keep in memory */
      }
      return next;
    });
  };

  // A regen entry links to its shot: seek the playhead there, which swaps the
  // cinema to that shot's clip and (under video ownership) follows in the page.
  const onSelectShot = (shotId: string) => {
    const span = shots?.find((s) => s.shot_id === shotId)?.source_span as
      | { word_range?: [number, number] }
      | null
      | undefined;
    const startWord = span?.word_range?.[0];
    if (typeof startWord === "number") engine.seek(startWord, performance.now());
  };

  const toggleBookmark = () => {
    if (!bookId) return;
    setBookmarks((prev) => {
      const next = { ...prev };
      if (next[bookId] !== undefined && next[bookId] === visiblePage) delete next[bookId];
      else next[bookId] = visiblePage;
      try {
        localStorage.setItem(BOOKMARK_KEY, JSON.stringify(next));
      } catch {
        /* private mode — keep in memory */
      }
      return next;
    });
  };

  const onShare = () => {
    const link = `kinora://book/${bookId}`;
    void navigator.clipboard?.writeText(link).catch(() => undefined);
    setShareConfirmed(true);
    window.setTimeout(() => setShareConfirmed(false), 1600);
  };

  if (!bookId) return null;

  if (book && !bookIsOpenable(book)) {
    const { title, body } = importGateMessage(book);
    return (
      <div className="flex h-screen flex-col items-center justify-center bg-walnut-deep px-8 text-center text-parchment">
        <h1 className="font-display text-2xl">{title}</h1>
        <p className="mt-3 max-w-md text-sm text-white/65">{body}</p>
        <p className="mt-2 font-display text-white/85">{book.title}</p>
        <button
          type="button"
          onClick={() => navigate("/")}
          className="mt-6 rounded-xl bg-white/[0.12] px-5 py-2.5 text-sm font-medium text-white hover:bg-white/20"
        >
          Back to the shelf
        </button>
      </div>
    );
  }

  const bookmarked = bookmarks[bookId] === visiblePage;

  return (
    <div className="flex h-screen flex-col bg-walnut font-sans text-parchment">
      <ReadingToolbar
        title={book?.title ?? "Reading"}
        author={book?.author ?? null}
        reading={reading}
        bookmarked={bookmarked}
        onToggleBookmark={toggleBookmark}
        search={search}
        onSearch={setSearch}
        onBack={() => navigate("/")}
        onShare={onShare}
        shareConfirmed={shareConfirmed}
        onOpenMetrics={() => setMetricsOpen(true)}
        metricsOpen={metricsOpen}
        onOpenCanon={() => setCanonOpen(true)}
        canonOpen={canonOpen}
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <section className="min-h-0 border-r border-white/10">
          <PdfReadingColumn
            bookId={bookId}
            numPages={book?.num_pages ?? null}
            page={enginePage}
            autoFollow={snapshot.owner === "video"}
            highlightWordIndex={snapshot.owner === "video" ? snapshot.highlightWordIndex : null}
            settings={reading.settings}
            theme={reading.theme}
            onSeekWord={(word) => engine.seek(word, performance.now())}
            onReportScroll={(word) => engine.reportScroll(word, performance.now())}
            onVisiblePageChange={setVisiblePage}
          />
        </section>

        <section className="relative hidden min-h-0 lg:block">
          <CinemaPanel
            engine={engine}
            clipUrl={snapshot.currentClipUrl}
            sourceId={snapshot.currentSource?.id ?? null}
            playheadSeekS={snapshot.playheadSeekS}
            playheadSeekSeq={snapshot.playheadSeekSeq}
            nextSource={snapshot.nextSource}
            stage={snapshot.currentStage}
            keyframeUrl={snapshot.currentKeyframeUrl}
            illustrationUrl={snapshot.currentIllustrationUrl}
            beatId={snapshot.currentBeatId}
            underBudgetPressure={snapshot.underBudgetPressure}
            isPlaying={snapshot.isPlaying}
            mode={snapshot.mode}
            onToggleMode={() => engine.setMode(snapshot.mode === "viewer" ? "director" : "viewer")}
            socketStatus={socketStatus}
            budgetRemaining={budgetRemaining}
            activity={activity}
            sceneShots={sceneShots}
            currentShotId={snapshot.currentShotId}
            onSeekShot={(shot) => engine.seek(shot.startWord, performance.now())}
            onSendComment={async (note, regionPng) => {
              const shotId = snapshot.currentShotId;
              const res = await sendComment(note, shotId, regionPng);
              if (res && shotId) {
                history.record(shotId, { note, agent: res.agent, aspect: res.aspect, at: Date.now() });
              }
              return res;
            }}
            directionCounts={history.counts}
            directions={history.recentFor(snapshot.currentShotId)}
            loadingShots={shots === undefined}
          />
          <BufferIndicator
            sessionId={sessionId}
            bufferState={bufferState}
            focusWord={snapshot.focusWord}
            velocity={snapshot.velocity}
            stage={snapshot.currentStage}
            budgetLow={snapshot.underBudgetPressure}
          />
          <AgentActivityFeed
            activity={activity}
            socketStatus={socketStatus}
            open={feedOpen}
            onToggle={toggleFeed}
            onSelectShot={onSelectShot}
            onResolveConflict={(c) => reopenConflict(c.conflictId)}
          />
        </section>
      </div>

      {metricsOpen && (
        <MetricsPanel
          bookId={bookId}
          sessionId={sessionId}
          bookTitle={book?.title ?? null}
          liveSignal={snapshot.focusWord}
          onClose={() => setMetricsOpen(false)}
        />
      )}

      {canonOpen && (
        <CanonEditorPanel
          bookId={bookId}
          shots={shots}
          shotUpdates={shotUpdates}
          lastEdit={canonRegen.lastEdit}
          onEditApplied={(result) => {
            // Mark the dependent shots regenerating in the shared map so the
            // Director timeline shows them re-rendering too (§5.4/§8.7).
            canonRegen.registerEdit(result);
            markRegenerating(result.affectedShotIds);
          }}
          onClose={() => setCanonOpen(false)}
        />
      )}

      <ConflictDialog
        conflict={activeConflict}
        trace={conflictTrace}
        shotClipUrl={disputedClipUrl}
        onResolve={resolveConflict}
        onDismiss={dismissConflict}
      />
    </div>
  );
}
