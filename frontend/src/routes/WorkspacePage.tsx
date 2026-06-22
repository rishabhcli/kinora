import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  ApiError,
  books as booksApi,
  eventStreamUrl,
  sessions,
  websocketUrl,
} from "../api/client";
import type { Book, CanonGraph, KinoraEvent, Shot } from "../api/types";
import { BrandMark, Wordmark } from "../components/common/BrandMark";
import { ChartIcon, Spinner } from "../components/common/icons";
import { PdfReader } from "../components/workspace/PdfReader";
import { SplitPane } from "../components/workspace/SplitPane";
import { VideoStage } from "../components/workspace/VideoStage";
import { useEventsStore } from "../stores/eventsStore";
import { useSessionStore } from "../stores/sessionStore";
import { GenerationClient } from "../sync/GenerationClient";
import { SyncEngine } from "../sync/SyncEngine";
import { useSyncSnapshot } from "../sync/useSyncEngine";

const MetricsPanel = lazy(() =>
  import("../metrics/MetricsPanel").then((m) => ({ default: m.MetricsPanel })),
);

function WorkspaceHeader({
  book,
  engine,
  onOpenMetrics,
}: {
  book: Book;
  engine: SyncEngine | null;
  onOpenMetrics: () => void;
}) {
  return (
    <header className="flex items-center justify-between gap-4 border-b border-kinora-line/60 bg-kinora-ink/70 px-4 py-2.5 backdrop-blur">
      <div className="flex min-w-0 items-center gap-3">
        <Link to="/" aria-label="Back to library">
          <Wordmark />
        </Link>
        <span className="hidden text-kinora-line sm:inline">/</span>
        <span className="hidden min-w-0 truncate text-sm text-kinora-mist sm:inline">
          {book.title}
        </span>
      </div>
      <div className="flex items-center gap-2">
        {engine ? <ReadingReadout engine={engine} /> : null}
        <button
          type="button"
          onClick={onOpenMetrics}
          className="inline-flex items-center gap-1.5 rounded-full border border-kinora-line px-3 py-1.5 text-xs font-medium text-kinora-mist transition-colors hover:border-kinora-iris/60 hover:bg-white/5"
        >
          <ChartIcon className="h-3.5 w-3.5" /> Metrics
        </button>
      </div>
    </header>
  );
}

function ReadingReadout({ engine }: { engine: SyncEngine }) {
  const snap = useSyncSnapshot(engine);
  return (
    <span className="hidden items-center gap-3 rounded-full border border-kinora-line px-3 py-1.5 text-[0.7rem] text-kinora-muted lg:inline-flex">
      <span title="control owner">
        <span className="text-kinora-iris">{snap.owner}</span> owns
      </span>
      <span className="tabular-nums" title="reading velocity">
        {snap.velocity.toFixed(1)} wps
      </span>
      <span className="tabular-nums" title="committed buffer">
        {Math.round(snap.committedSecondsAhead)}s
      </span>
    </span>
  );
}

function CenterMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-4 px-6 text-center text-kinora-muted">
      <BrandMark className="h-10 w-10 motion-safe:animate-pulse-glow" />
      {children}
    </div>
  );
}

export default function WorkspacePage() {
  const { id = "" } = useParams();
  const [book, setBook] = useState<Book | null>(null);
  const [bookError, setBookError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [engine, setEngine] = useState<SyncEngine | null>(null);
  const [shots, setShots] = useState<Shot[]>([]);
  const [canon, setCanon] = useState<CanonGraph | null>(null);
  const [metricsOpen, setMetricsOpen] = useState(false);

  const sessionForBook = useRef<string | null>(null);
  const setSessionStore = useSessionStore((s) => s.setSession);
  const resetSessionStore = useSessionStore((s) => s.reset);
  const resetEvents = useEventsStore((s) => s.reset);
  const setConnection = useEventsStore((s) => s.setConnection);
  const pushEvent = useEventsStore((s) => s.push);

  // 1. Load the book; poll while it is still importing.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = async () => {
      try {
        const b = await booksApi.get(id);
        if (cancelled) return;
        setBook(b);
        setBookError(null);
        if (b.status === "importing") timer = setTimeout(load, 2000);
      } catch (err) {
        if (cancelled) return;
        setBookError(err instanceof ApiError ? err.message : "Could not load this book.");
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [id]);

  // 2. Once ready, create a session + load shots & canon (once per book).
  const bookReady = book?.status === "ready";
  useEffect(() => {
    if (!bookReady || !book) return;
    if (sessionForBook.current === book.id) return;
    sessionForBook.current = book.id;
    let cancelled = false;
    (async () => {
      try {
        const { session_id } = await sessions.create(book.id);
        if (cancelled) return;
        setSessionId(session_id);
        setSessionStore(session_id, book.id);
      } catch {
        sessionForBook.current = null;
      }
    })();
    void booksApi
      .getShots(book.id)
      .then((s) => !cancelled && setShots(s))
      .catch(() => undefined);
    void booksApi
      .getCanon(book.id)
      .then((c) => !cancelled && setCanon(c))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [bookReady, book, setSessionStore]);

  // 3. Build the SyncEngine + GenerationClient for the session; tear down on exit.
  useEffect(() => {
    if (!sessionId) return undefined;
    const eng = new SyncEngine({
      sessionId,
      pushIntent: (intent) => {
        sessions.intent(sessionId, intent).catch(() => undefined);
      },
      postSeek: (word) => {
        sessions.seek(sessionId, word).catch(() => undefined);
      },
    });

    const onEvent = (event: KinoraEvent) => {
      pushEvent(event);
      switch (event.type) {
        case "clip_ready":
          eng.registerClip(event.data.shot_id, event.data.oss_url, event.data.sync_segment);
          break;
        case "keyframe_ready":
          eng.registerKeyframe(event.data.beat_id, event.data.oss_url, event.data.shot_id);
          break;
        case "scene_stitched":
          eng.registerScene(event.data.sync_map, event.data.oss_url);
          break;
        case "regen_done":
          eng.registerRegen(event.data.shot_id, event.data.oss_url);
          break;
        case "budget_low":
          eng.setBudgetRemaining(event.data.remaining_s);
          break;
        default:
          break;
      }
    };

    const gen = new GenerationClient({
      sessionId,
      eventsUrl: eventStreamUrl(sessionId),
      wsUrl: websocketUrl(sessionId),
      onEvent,
      onStatus: setConnection,
    });
    gen.connect();
    setEngine(eng);

    return () => {
      gen.close();
      eng.destroy();
      setEngine(null);
    };
  }, [sessionId, pushEvent, setConnection]);

  // Feed the shot list (source-span index) into the engine when it arrives.
  useEffect(() => {
    if (engine && shots.length) engine.setShots(shots);
  }, [engine, shots]);

  // Reset shared session/event stores on unmount.
  useEffect(
    () => () => {
      resetEvents();
      resetSessionStore();
    },
    [resetEvents, resetSessionStore],
  );

  const onCanonEdited = useCallback(
    (affected: string[]) => {
      if (!affected.length) return;
      // Mark affected shots as rendering until regen_done swaps them in.
      setShots((prev) =>
        prev.map((s) =>
          affected.includes(s.shot_id) ? { ...s, status: "rendering" as const } : s,
        ),
      );
    },
    [],
  );

  if (bookError) {
    return (
      <CenterMessage>
        <p className="text-sm">{bookError}</p>
        <Link to="/" className="text-sm font-medium text-kinora-iris hover:underline">
          Back to library
        </Link>
      </CenterMessage>
    );
  }

  if (!book) {
    return (
      <CenterMessage>
        <span className="inline-flex items-center gap-2 text-sm">
          <Spinner className="h-4 w-4" /> Opening book…
        </span>
      </CenterMessage>
    );
  }

  if (book.status === "failed") {
    return (
      <CenterMessage>
        <p className="text-sm text-kinora-danger">This book failed to import.</p>
        <Link to="/" className="text-sm font-medium text-kinora-iris hover:underline">
          Back to library
        </Link>
      </CenterMessage>
    );
  }

  if (book.status === "importing") {
    const pct = Math.round((book.progress <= 1 ? book.progress * 100 : book.progress) || 0);
    return (
      <CenterMessage>
        <p className="text-sm">
          Preparing <span className="text-kinora-mist">{book.title}</span> — {book.stage ?? "analysing"}…
        </p>
        <div className="h-1.5 w-56 overflow-hidden rounded-full bg-kinora-line">
          <div className="h-full rounded-full bg-kinora-glow transition-[width]" style={{ width: `${pct}%` }} />
        </div>
        <span className="text-xs tabular-nums">{pct}%</span>
      </CenterMessage>
    );
  }

  return (
    <div className="flex h-screen flex-col">
      <WorkspaceHeader book={book} engine={engine} onOpenMetrics={() => setMetricsOpen(true)} />
      <div className="min-h-0 flex-1">
        {engine && sessionId ? (
          <SplitPane
            left={<PdfReader bookId={book.id} numPages={book.num_pages} engine={engine} />}
            right={
              <VideoStage
                engine={engine}
                sessionId={sessionId}
                bookId={book.id}
                shots={shots}
                canon={canon}
                onCanonEdited={onCanonEdited}
              />
            }
          />
        ) : (
          <CenterMessage>
            <span className="inline-flex items-center gap-2 text-sm">
              <Spinner className="h-4 w-4" /> Starting your reading session…
            </span>
          </CenterMessage>
        )}
      </div>
      {metricsOpen ? (
        <Suspense fallback={null}>
          <MetricsPanel
            open
            onClose={() => setMetricsOpen(false)}
            bookId={book.id}
            sessionId={sessionId}
          />
        </Suspense>
      ) : null}
    </div>
  );
}
