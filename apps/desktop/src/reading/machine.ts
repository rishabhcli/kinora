// The open-state machine — WS1's core guarantee: from OPEN we ALWAYS progress to
// a revealable, playing film, no matter which async step fails. Pure and
// framework-free (no React, no DOM) so it is exhaustively unit-tested.
//
// States:
//   idle → opening(anim) → loading(meta/pages/shots) → warming(session+first
//   frame) → ready → reading → closing
//
// The open animation (anim) and the data load run in PARALLEL; the film is only
// revealed once BOTH the first frame is paintable AND the animation is ready —
// see canReveal(). Failures never dead-end: FALLBACK pivots to the bundled film,
// which always plays.

export type Phase =
  | "idle"
  | "opening"
  | "loading"
  | "warming"
  | "ready"
  | "reading"
  | "closing";

export type FilmMode = "unknown" | "live" | "fallback";

export interface LoadFlags {
  meta: boolean;
  pages: boolean;
  shots: boolean;
  session: boolean;
  /** The film surface is paintable — a real decoded frame OR a poster/keyframe. */
  firstFrame: boolean;
}

export interface MachineState {
  phase: Phase;
  mode: FilmMode;
  load: LoadFlags;
  /** The open animation has reached the point where it is safe to reveal. */
  animReady: boolean;
  /** A soft, recoverable note (we degraded to fallback). Never fatal. */
  error: string | null;
}

export type MachineEvent =
  | { type: "OPEN" }
  | { type: "META" }
  | { type: "PAGES" }
  | { type: "SHOTS" }
  | { type: "SESSION" }
  | { type: "FIRST_FRAME" }
  | { type: "ANIM_READY" }
  | { type: "FALLBACK"; message?: string }
  | { type: "REVEAL" }
  | { type: "CLOSE" }
  | { type: "CLOSED" };

export const initialState: MachineState = {
  phase: "idle",
  mode: "unknown",
  load: { meta: false, pages: false, shots: false, session: false, firstFrame: false },
  animReady: false,
  error: null,
};

// Forward-only ordering of the active phases. idle/closing sit outside this and
// are handled explicitly — load events are inert in those phases.
const RANK: Record<Phase, number> = {
  idle: 0,
  opening: 1,
  loading: 2,
  warming: 3,
  ready: 4,
  reading: 5,
  closing: 6,
};

const ACTIVE: readonly Phase[] = ["opening", "loading", "warming", "ready", "reading"];
const isActive = (p: Phase): boolean => ACTIVE.includes(p);

/** Advance to `target` only when actively open and not already past it. */
function bump(phase: Phase, target: Phase): Phase {
  if (!isActive(phase)) return phase;
  return RANK[target] > RANK[phase] ? target : phase;
}

export function reduce(state: MachineState, event: MachineEvent): MachineState {
  switch (event.type) {
    case "OPEN":
      // Hard reset — a fresh open from ANY phase (covers rapid open/close/open).
      return { ...initialState, phase: "opening" };
    case "CLOSE":
      return state.phase === "idle" ? state : { ...state, phase: "closing" };
    case "CLOSED":
      return initialState;
    default:
      break;
  }

  // Stray async results after teardown (closing/idle) are ignored so they cannot
  // resurrect a closed room.
  if (!isActive(state.phase)) return state;

  switch (event.type) {
    case "META":
      return { ...state, load: { ...state.load, meta: true }, phase: bump(state.phase, "loading") };
    case "PAGES":
      return { ...state, load: { ...state.load, pages: true } };
    case "SHOTS":
      return { ...state, load: { ...state.load, shots: true } };
    case "SESSION": {
      // Enter warming; if a frame is already painted, fast-forward to ready.
      const warming = bump(state.phase, "warming");
      return {
        ...state,
        // A prior FALLBACK wins — once degraded, stay on the bundled film.
        mode: state.mode === "fallback" ? "fallback" : "live",
        load: { ...state.load, session: true },
        phase: state.load.firstFrame ? bump(warming, "ready") : warming,
      };
    }
    case "FALLBACK": {
      const warming = bump(state.phase, "warming");
      return {
        ...state,
        mode: "fallback",
        error: event.message ?? state.error,
        phase: state.load.firstFrame ? bump(warming, "ready") : warming,
      };
    }
    case "FIRST_FRAME":
      // Only reveal once we've committed to a film source (warming); an eager
      // frame during opening/loading is recorded but keeps the warm-up up.
      return {
        ...state,
        load: { ...state.load, firstFrame: true },
        phase: state.phase === "warming" ? "ready" : state.phase,
      };
    case "ANIM_READY":
      return { ...state, animReady: true };
    case "REVEAL":
      return state.phase === "ready" ? { ...state, phase: "reading" } : state;
    default:
      return state;
  }
}

/** The film surface is paintable (decoded frame or poster). */
export function filmReady(s: MachineState): boolean {
  return s.load.firstFrame;
}

/** Safe to dissolve the cover into the film: frame painted AND animation ready. */
export function canReveal(s: MachineState): boolean {
  return s.phase === "ready" && s.animReady && s.load.firstFrame;
}
