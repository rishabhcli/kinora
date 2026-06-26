// The book-open film experience — the reading-room SHELL entry point. Owns the
// open-state machine and composes Agent 4's open transition, Agent 2's scroll
// film engine, and Agent 6's reading controls (all via producers.tsx) into one
// flawless open→read→close whole that is fully functional every time, even with
// KINORA_LIVE_VIDEO OFF. See coordination/CONTRACTS.md.
import { AnimatePresence, useReducedMotion } from "framer-motion";
import { useEffect, useReducer, useRef } from "react";
import type { Book } from "../data/books";
import { BookOpenTransition } from "./producers";
import { ReadingRoomShell } from "./ReadingRoomShell";
import { useFilmSession } from "./useFilmSession";
import { canReveal, initialState, reduce as machineReduce } from "./machine";

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
  const reduceMotion = !!useReducedMotion();
  const session = useFilmSession(book, dispatch);
  const prevId = useRef<string | null>(null);

  // OPEN on a new book; CLOSE when it goes away (handles rapid open/close).
  useEffect(() => {
    const id = book?.id ?? null;
    if (id && id !== prevId.current) dispatch({ type: "OPEN" });
    else if (!id && prevId.current) dispatch({ type: "CLOSE" });
    prevId.current = id;
  }, [book]);

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

  return (
    <AnimatePresence onExitComplete={() => dispatch({ type: "CLOSED" })}>
      {book && (
        <BookOpenTransition
          key={book.id}
          originRect={originRect}
          cover={{ image: book.coverImage, gradient: book.coverGradient, title: book.title }}
          reduce={reduceMotion}
          onOpened={() => dispatch({ type: "ANIM_READY" })}
          onClosed={() => dispatch({ type: "CLOSED" })}
        >
          <ReadingRoomShell book={book} onClose={onClose} state={state} dispatch={dispatch} session={session} reduce={reduceMotion} />
        </BookOpenTransition>
      )}
    </AnimatePresence>
  );
}
