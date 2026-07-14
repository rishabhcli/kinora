// The book-open film experience — the reading-room SHELL entry point. Owns the
// open-state machine and composes the open transition, scroll-film engine, and
// reading controls (all via producers.tsx) into one
// flawless open→read→close whole that is fully functional every time, even with
// KINORA_LIVE_VIDEO OFF.
// This is the product promise in code: not "play a clip beside text", but a book
// opening into a responsive film room while still behaving like a dependable
// reader.
import { AnimatePresence } from "framer-motion";
import { useCallback, useEffect, useReducer, useRef } from "react";
import type { Book } from "../data/books";
import { BookOpenTransition } from "./producers";
import { ReadingRoomShell } from "./ReadingRoomShell";
import { useFilmSession } from "./useFilmSession";
import { canReveal, initialState, reduce as machineReduce } from "./machine";
import { useReducedMotionPref } from "../a11y/useReducedMotionPref";

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
  const session = useFilmSession(book, dispatch);

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

  // Safety net: a hung load never freezes the reader. It reveals an honest empty
  // film surface while keeping the text available.
  useEffect(() => {
    if (state.phase !== "opening" && state.phase !== "loading") return;
    const t = window.setTimeout(() => dispatch({ type: "FALLBACK", message: "Film is still rendering" }), 7000);
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
          <ReadingRoomShell book={book} onClose={onClose} state={state} session={session} reduce={reduceMotion} />
        </BookOpenTransition>
      )}
    </AnimatePresence>
  );
}
