import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { CinemaPanel } from "../components/reader/CinemaPanel";
import { ReadingColumn } from "../components/reader/ReadingColumn";
import { ReadingToolbar } from "../components/reader/ReadingToolbar";
import { useSyncEngine } from "../hooks/useSyncEngine";
import { api } from "../lib/api";
import { useReadingTheme } from "../lib/readingTheme";

const BOOKMARK_KEY = "kinora.bookmarks.v1";

function loadBookmarks(): Record<string, number> {
  if (typeof localStorage === "undefined") return {};
  try {
    return JSON.parse(localStorage.getItem(BOOKMARK_KEY) ?? "{}");
  } catch {
    return {};
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
  // A reader-chosen page that temporarily overrides the playhead's page (manual
  // page turns); cleared when playback advances to a new page.
  const [pageOverride, setPageOverride] = useState<number | null>(null);
  const [bookmarks, setBookmarks] = useState<Record<string, number>>(loadBookmarks);

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

  const { engine, snapshot, activity, budgetRemaining, sendComment } = useSyncEngine(sessionId);

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

  // When the playhead's page changes (playback / a seek), follow it and drop any
  // manual override so the page and film stay in lockstep.
  const enginePage = snapshot.currentPage || 1;
  const lastEnginePage = useRef(enginePage);
  useEffect(() => {
    if (enginePage !== lastEnginePage.current) {
      lastEnginePage.current = enginePage;
      setPageOverride(null);
    }
  }, [enginePage]);

  const displayPage = pageOverride ?? enginePage;

  const toggleBookmark = () => {
    if (!bookId) return;
    setBookmarks((prev) => {
      const next = { ...prev };
      if (next[bookId] !== undefined && next[bookId] === displayPage) delete next[bookId];
      else next[bookId] = displayPage;
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

  // A small eyebrow above the title — the book's art direction reads as a
  // tasteful "edition" line; the footer owns the page count.
  const chapterLabel = book?.art_direction?.trim() ? book.art_direction : "Now reading";

  if (!bookId) return null;

  const bookmarked = bookmarks[bookId] === displayPage;

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
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <section className="min-h-0 border-r border-white/10">
          <ReadingColumn
            bookId={bookId}
            page={displayPage}
            numPages={book?.num_pages ?? null}
            title={book?.title ?? "Reading"}
            chapterLabel={chapterLabel}
            highlightWordIndex={pageOverride === null ? snapshot.highlightWordIndex : null}
            settings={reading.settings}
            theme={reading.theme}
            onSeekWord={(word) => {
              setPageOverride(null);
              engine.seek(word, performance.now());
            }}
            onTurnPage={(next) => setPageOverride(Math.max(1, next))}
          />
        </section>

        <section className="hidden min-h-0 lg:block">
          <CinemaPanel
            engine={engine}
            clipUrl={snapshot.currentClipUrl}
            isPlaying={snapshot.isPlaying}
            mode={snapshot.mode}
            onToggleMode={() => engine.setMode(snapshot.mode === "viewer" ? "director" : "viewer")}
            activity={activity}
            budgetRemaining={budgetRemaining}
            onComment={(note) => sendComment(note, snapshot.currentShotId)}
          />
        </section>
      </div>
    </div>
  );
}
