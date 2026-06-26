// Drives the open-state machine through the data load and the live session:
//   meta → pages(≤60) → shots → createSession → openSessionEvents(SSE) →
//   postIntent(0) (prime the scheduler) → clips/buffer/crew stream in.
// Every failure path dispatches FALLBACK so the room degrades to the bundled film
// instead of erroring. Tears down the SSE on unmount/book-change (EventSource
// auto-reconnects across transient drops on its own).
import { useEffect, useRef, useState } from "react";
import { api, toBrowserUrl, type SessionEvent, type ShotResponse } from "../lib/api";
import type { Book } from "../data/books";
import { fallbackFilmFor } from "./fallback";
import type { MachineEvent } from "./machine";
import type { PageText } from "./slots";

export interface CrewActivity {
  id: number;
  agent: string;
  message: string;
}

export interface FilmSession {
  pages: PageText[];
  shots: ShotResponse[];
  clipByShot: Record<string, string>;
  sessionId: string | null;
  live: boolean;
  fallbackFilm: string;
  bufferAhead: number | null;
  bursting: boolean;
  inflight: { committed: number; speculative: number } | null;
  zone: string | null;
  crew: CrewActivity[];
}

// buffer_state carries more than api.ts's BufferState subset — read it defensively.
interface BufferStateMsg {
  committed_seconds_ahead?: number;
  bursting?: boolean;
  inflight_committed?: number;
  inflight_speculative?: number;
  zone?: string;
}
interface AgentActivityMsg {
  agent?: string;
  message?: string;
}

export function useFilmSession(book: Book | null, dispatch: (e: MachineEvent) => void): FilmSession {
  const [pages, setPages] = useState<PageText[]>([]);
  const [shots, setShots] = useState<ShotResponse[]>([]);
  const [clipByShot, setClipByShot] = useState<Record<string, string>>({});
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [bufferAhead, setBufferAhead] = useState<number | null>(null);
  const [bursting, setBursting] = useState(false);
  const [inflight, setInflight] = useState<{ committed: number; speculative: number } | null>(null);
  const [zone, setZone] = useState<string | null>(null);
  const [crew, setCrew] = useState<CrewActivity[]>([]);
  const crewId = useRef(0);

  const fallbackFilm = fallbackFilmFor(book?.id ?? "");

  useEffect(() => {
    if (!book) return;
    // Fresh slate for the new book.
    setPages([]);
    setShots([]);
    setClipByShot({});
    setSessionId(null);
    setLive(false);
    setBufferAhead(null);
    setBursting(false);
    setInflight(null);
    setZone(null);
    setCrew([]);

    // No auth → no backend session at all → straight to the bundled film.
    if (!api.isAuthed()) {
      dispatch({ type: "FALLBACK", message: "Offline preview" });
      return;
    }

    let alive = true;
    let closeEvents: (() => void) | null = null;

    (async () => {
      try {
        const meta = await api.getBook(book.id); // 404 for a mock book → throws → fallback
        if (!alive) return;
        dispatch({ type: "META" });

        // Fetch pages in bounded-parallel batches — a sequential loop over up to
        // 60 pages can exceed the 7s loading safety-net on a healthy-but-slow
        // backend and wrongly downgrade live content to the fallback film.
        const np = Math.min(meta.num_pages ?? 1, 60);
        const nums = Array.from({ length: np }, (_, i) => i + 1);
        const ps: PageText[] = [];
        const BATCH = 8;
        for (let i = 0; i < nums.length; i += BATCH) {
          if (!alive) return;
          const batch = await Promise.all(
            nums.slice(i, i + BATCH).map(async (n) => {
              try {
                const p = await api.getPage(book.id, n);
                return p.text ? { n, text: p.text } : null;
              } catch {
                return null; // page not rendered yet
              }
            }),
          );
          for (const p of batch) if (p) ps.push(p);
        }
        if (!alive) return;
        ps.sort((a, b) => a.n - b.n); // batches preserve order, but be defensive
        dispatch({ type: "PAGES" });

        const sh = (await api.getShots(book.id))
          .filter((s) => s.source_span)
          .sort((a, b) => a.source_span!.word_range[0] - b.source_span!.word_range[0]);
        if (!alive) return;

        // Backend reachable but the book isn't analysed yet → preview now, not an error.
        if (ps.length === 0 || sh.length === 0) {
          dispatch({ type: "FALLBACK", message: "Still preparing this book" });
          return;
        }
        dispatch({ type: "SHOTS" });
        setPages(ps);
        setShots(sh);
        const seed: Record<string, string> = {};
        for (const s of sh) if (s.clip_url) seed[s.shot_id] = toBrowserUrl(s.clip_url);
        setClipByShot(seed);
        setLive(true);

        const sess = await api.createSession(book.id, 0);
        if (!alive) return;
        setSessionId(sess.session_id);
        dispatch({ type: "SESSION" });

        closeEvents = api.openSessionEvents(sess.session_id, (e: SessionEvent) => {
          if (!alive) return;
          switch (e.event) {
            case "clip_ready": {
              const c = e as unknown as { shot_id: string; oss_url: string };
              if (c.oss_url) setClipByShot((m) => ({ ...m, [c.shot_id]: toBrowserUrl(c.oss_url) }));
              break;
            }
            case "buffer_state": {
              const b = e as unknown as BufferStateMsg;
              setBufferAhead(b.committed_seconds_ahead ?? null);
              setBursting(Boolean(b.bursting));
              if (typeof b.inflight_committed === "number" || typeof b.inflight_speculative === "number") {
                setInflight({ committed: b.inflight_committed ?? 0, speculative: b.inflight_speculative ?? 0 });
              }
              if (typeof b.zone === "string") setZone(b.zone);
              break;
            }
            case "agent_activity": {
              const a = e as unknown as AgentActivityMsg;
              if (a.message) {
                const id = ++crewId.current;
                const agent = a.agent ?? "Crew";
                const message = a.message;
                setCrew((c) => [...c.slice(-5), { id, agent, message }]);
              }
              break;
            }
            case "scene_stitched": {
              const id = ++crewId.current;
              setCrew((c) => [...c.slice(-5), { id, agent: "Editor", message: "Scene stitched" }]);
              break;
            }
            default:
              break;
          }
        });
        api.postIntent(sess.session_id, 0, 4).catch(() => {}); // prime the scheduler
      } catch {
        if (alive) dispatch({ type: "FALLBACK", message: "Showing a preview film" });
      }
    })();

    return () => {
      alive = false;
      closeEvents?.();
      setSessionId(null);
    };
  }, [book, dispatch]);

  return { pages, shots, clipByShot, sessionId, live, fallbackFilm, bufferAhead, bursting, inflight, zone, crew };
}
