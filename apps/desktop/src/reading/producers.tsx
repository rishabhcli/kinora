// Producer wiring for the reading-room slots. The shell composes these three
// components behind the contracts in slots.ts. Today they are built-in stand-ins
// (reading/builtin/) so the room is fully functional on its own.
//
// ── INTEGRATION (Agent 12) ──────────────────────────────────────────────────
// When each producer's PR merges, swap the import here — nothing else in the
// reading lane changes. The real components implement the same slot props:
//   ScrollFilmEngine    → Agent 2  src/reading/ScrollFilmEngine.tsx
//   ReadingControls     → Agent 6  src/reading/ReadingControls.tsx
//   BookOpenTransition  → Agent 4  src/motion/BookOpenTransition.tsx
// ────────────────────────────────────────────────────────────────────────────
import { BuiltinScrollFilmEngine } from "./builtin/BuiltinFilm";
import { BuiltinReadingControls } from "./builtin/BuiltinControls";
import { BuiltinBookOpenTransition } from "./builtin/BuiltinOpenTransition";
import type {
  ScrollFilmEngineComponent,
  ReadingControlsComponent,
  BookOpenTransitionComponent,
} from "./slots";

export const ScrollFilmEngine: ScrollFilmEngineComponent = BuiltinScrollFilmEngine;
export const ReadingControls: ReadingControlsComponent = BuiltinReadingControls;
export const BookOpenTransition: BookOpenTransitionComponent = BuiltinBookOpenTransition;
