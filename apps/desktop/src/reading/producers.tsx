// Producer wiring for the reading-room slots. The shell composes these three
// components behind the contracts in slots.ts.
//
// ── INTEGRATION (Agent 12) ──────────────────────────────────────────────────
// Real producers wired in per A10's documented swap (adapters live HERE only):
//   ScrollFilmEngine → Agent 2 (reading/ScrollFilmEngine.tsx). Slot→real prop deltas:
//       `reduce` → `reducedMotion`, `clipByShot` → `clips`; A2 has no `onFirstFrame`
//       (the shell's warm-up safety timeout dispatches FIRST_FRAME — A10's note).
//   ReadingControls  → Agent 6 (reading/ReadingControls.tsx). Drop the slot's `reduce`
//       (A6 reads reduced-motion itself via useReducedMotionPref()).
//   BookOpenTransition → KEEP A10's builtin in-room hinge-open reveal (A10 option a):
//       A4's <BookOpenTransition> does the complementary shelf→center TRAVEL and wraps
//       at the HomePage level — not a drop-in for the in-room reveal slot.
// ────────────────────────────────────────────────────────────────────────────
import { ScrollFilmEngine as RealScrollFilmEngine } from "./ScrollFilmEngine";
import { ReadingControls as RealReadingControls } from "./ReadingControls";
import { BuiltinBookOpenTransition } from "./builtin/BuiltinOpenTransition";
import type {
  ScrollFilmEngineComponent,
  ReadingControlsComponent,
  BookOpenTransitionComponent,
} from "./slots";

export const ScrollFilmEngine: ScrollFilmEngineComponent = (props) => (
  <RealScrollFilmEngine
    book={props.book}
    pages={props.pages}
    shots={props.shots}
    sessionId={props.sessionId}
    live={props.live}
    prefs={props.prefs}
    clips={props.clipByShot}
    reducedMotion={props.reduce}
    onProgress={props.onProgress}
  />
);

export const ReadingControls: ReadingControlsComponent = (props) => (
  <RealReadingControls prefs={props.prefs} onChange={props.onChange} />
);

export const BookOpenTransition: BookOpenTransitionComponent = BuiltinBookOpenTransition;
