// The book-open film experience — the reading-room SHELL entry point. Owns the
// open-state machine and composes Agent 4's open transition, Agent 2's scroll
// film engine, and Agent 6's reading controls (all via producers.tsx) into one
// flawless open→read→close whole that is fully functional every time, even with
// KINORA_LIVE_VIDEO OFF. See coordination/CONTRACTS.md.
import { AnimatePresence } from "framer-motion";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type { Book } from "../data/books";
import { BookOpenTransition } from "./producers";
import { ReadingRoomShell } from "./ReadingRoomShell";
import { useFilmSession } from "./useFilmSession";
import { canReveal, initialState, reduce as machineReduce } from "./machine";
import { useReducedMotionPref } from "../a11y/useReducedMotionPref";

// Persisted across sessions: whether the user has explicitly enabled AI video
// generation. Default OFF — the reader sees the bundled fallback film and the
// text content immediately, with a clear toggle in the top bar to opt in.
const GEN_KEY = "kinora.reading.generateVideo";
function readGenPref(): boolean {
  try {
    return localStorage.getItem(GEN_KEY) === "1";
  } catch {
    return false;
  }
}

export default function ReadingRoom({
  book,
  onClose,
  originRect,
}: {
  book: Book | null;
  onClose: () => void;
  originRect?: DOMRect | null;
}) {
  const [state, dispatch] = useReducer(machineReduce, initialState);
  const reduceMotion = useReducedMotionPref();
  const prevId = useRef<string | null>(null);
  const [generateVideo, setGenerateVideo] = useState<boolean>(readGenPref);
  const onToggleGenerate = useCallback((next: boolean) => {
    setGenerateVideo(next);
    try {
      localStorage.setItem(GEN_KEY, next ? "1" : "0");
    } catch { /* storage blocked */ }
  }, []);

  // OPEN on a new book; CLOSE when it goes away (handles rapid open/close).
  // MUST be declared before useFilmSession so its effect runs FIRST — otherwise
  // the loader dispatches FALLBACK/META while the machine is still idle (which it
  // ignores for teardown safety) and we'd strand in `opening`.
  useEffect(() => {
    const id = book?.id ?? null;
    if (id && id !== prevId.current) dispatch({ type: "OPEN" });
    else if (!id && prevId.current) dispatch({ type: "CLOSE" });
    prevId.current = id;
  }, [book]);

  // The data + live session loader (dispatches META/PAGES/SHOTS/SESSION/FALLBACK).
  const session = useFilmSession(book, dispatch, generateVideo);

  // Reveal once the film frame is painted AND the open animation is ready.
  useEffect(() => {
    if (canReveal(state)) dispatch({ type: "REVEAL" });
  }, [state]);

  // Safety net: warming never hangs — reveal to the poster after a beat even if
  // no canplay/loadeddata frame callback ever fires (missing asset, slow decode).
  useEffect(() => {
    if (state.phase !== "warming") return;
    const t = window.setTimeout(() => dispatch({ type: "FIRST_FRAME" }), 2600);
    return () => window.clearTimeout(t);
  }, [state.phase]);

  // Safety net: a hung load (backend stalls with no error) never freezes — fall
  // back to the bundled film after a generous beat. Normal ingest progress shows
  // the warm-up until then.
  useEffect(() => {
    if (state.phase !== "opening" && state.phase !== "loading") return;
    const t = window.setTimeout(() => dispatch({ type: "FALLBACK", message: "Showing a preview film" }), 7000);
    return () => window.clearTimeout(t);
  }, [state.phase]);

  // Stable callbacks — an unstable identity would reset the transition's open
  // timer (and the engine's scroll listener) on every SSE-driven re-render.
  const onOpened = useCallback(() => dispatch({ type: "ANIM_READY" }), []);
  const onClosed = useCallback(() => dispatch({ type: "CLOSED" }), []);

  return (
    <AnimatePresence onExitComplete={() => dispatch({ type: "CLOSED" })}>
      {book && (
        <BookOpenTransition
          key={book.id}
          originRect={originRect}
          cover={{ image: book.coverImage, gradient: book.coverGradient, title: book.title }}
          reduce={reduceMotion}
          onOpened={onOpened}
          onClosed={onClosed}
        >
          <ReadingRoomShell book={book} onClose={onClose} state={state} dispatch={dispatch} session={session} reduce={reduceMotion} generateVideo={generateVideo} onToggleGenerate={onToggleGenerate} />
        </BookOpenTransition>
      )}
    </AnimatePresence>
  );
}
