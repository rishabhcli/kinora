// The reading-room slot contract. The shell composes three producer components
// behind these stable interfaces; built-in stand-ins (reading/builtin/) satisfy
// them today, and Agent 12 swaps in the real Agent 2/4/6 components at
// integration (see coordination/CONTRACTS.md). Types only — no runtime.
import type { ComponentType, ReactNode } from "react";
import type { Book } from "../data/books";
import type { ShotResponse } from "../lib/api";
import type { ReadingPrefs } from "../lib/readingPrefs";

/** One page of book text. */
export interface PageText {
  n: number;
  text: string;
}

/** Slot — Agent 2 `<ScrollFilmEngine>`: the vertical film (never-black crossfade)
 *  + the scrolling text column + scroll→focus-word→scheduler wiring. */
export interface ScrollFilmEngineProps {
  book: Book;
  pages: PageText[];
  shots: ShotResponse[];
  sessionId: string | null;
  clipByShot: Record<string, string>;
  fallbackFilm: string;
  live: boolean;
  prefs: ReadingPrefs;
  reduce: boolean;
  /** Scroll position (0..1) + the focus word it maps to — drives the shell's rail. */
  onProgress?: (frac: number, focusWord: number) => void;
  /** The film surface is paintable (real frame or poster) → machine FIRST_FRAME. */
  onFirstFrame?: () => void;
}
export type ScrollFilmEngineComponent = ComponentType<ScrollFilmEngineProps>;

/** Slot — Agent 6 `<ReadingControls prefs onChange />`: controlled by the shell's
 *  single useReadingPrefs() instance (so the engine + controls stay in sync —
 *  separate hook instances would not). Mounted in the top bar. */
export interface ReadingControlsProps {
  prefs: ReadingPrefs;
  onChange: (p: Partial<ReadingPrefs>) => void;
  /** Reduced motion — drop popover transitions. */
  reduce?: boolean;
}
export type ReadingControlsComponent = ComponentType<ReadingControlsProps>;

/** Wrapper — Agent 4 `<BookOpenTransition>`: the open/close choreography. */
export interface BookOpenTransitionProps {
  /** The tapped cover's on-shelf rect (for the lift). Omit → animate from center. */
  originRect?: DOMRect | null;
  cover: { image?: string; gradient?: string; title?: string };
  reduce: boolean;
  /** Open choreography reached its reveal point → machine ANIM_READY. */
  onOpened?: () => void;
  /** Close choreography finished → machine CLOSED (then the parent unmounts). */
  onClosed?: () => void;
  children: ReactNode;
}
export type BookOpenTransitionComponent = ComponentType<BookOpenTransitionProps>;
