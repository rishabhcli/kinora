// Hover-intent preview state machine — the pure core behind the rich hover/
// preview interaction on a book card. A card must be hovered for `openDelayMs`
// before the preview opens (so a fast mouse sweep across a row doesn't flicker
// open), and a brief grace `closeDelayMs` before it closes (so moving the
// pointer into the preview itself doesn't dismiss it).
//
// The machine is a pure reducer over (state, event, now) → state + the timer the
// host should arm. The React layer owns the actual setTimeout; this keeps timing
// logic deterministic and testable with virtual time.

export interface PreviewState {
  /** The id currently hovered (intent pending), or null. */
  pendingId: string | null;
  /** When the pending hover started (epoch ms). */
  pendingSince: number | null;
  /** The id whose preview is open, or null. */
  openId: string | null;
  /** When a close was requested (epoch ms), or null if not closing. */
  closingSince: number | null;
}

export type PreviewEvent =
  | { type: "enter"; id: string }
  | { type: "leave"; id: string }
  | { type: "enterPreview" } // pointer moved into the open preview panel
  | { type: "leavePreview" }
  | { type: "tick" } // a timer fired; re-evaluate against `now`
  | { type: "dismiss" }; // force close (Escape, open the book, etc.)

export interface PreviewConfig {
  openDelayMs: number;
  closeDelayMs: number;
}

export const DEFAULT_PREVIEW_CONFIG: PreviewConfig = { openDelayMs: 450, closeDelayMs: 180 };

export function initialPreviewState(): PreviewState {
  return { pendingId: null, pendingSince: null, openId: null, closingSince: null };
}

/** The next timer the host should arm, in ms from `now`, or null if none. */
export interface PreviewResult {
  state: PreviewState;
  /** Arm a single timer this many ms in the future (null = disarm). */
  armInMs: number | null;
}

export function reducePreview(
  state: PreviewState,
  event: PreviewEvent,
  now: number,
  config: PreviewConfig = DEFAULT_PREVIEW_CONFIG,
): PreviewResult {
  switch (event.type) {
    case "enter": {
      // Hovering the already-open card cancels any pending close.
      if (state.openId === event.id) {
        return { state: { ...state, closingSince: null, pendingId: null, pendingSince: null }, armInMs: null };
      }
      // Begin (or restart) intent timing for this card.
      return {
        state: { ...state, pendingId: event.id, pendingSince: now, closingSince: null },
        armInMs: config.openDelayMs,
      };
    }

    case "leave": {
      // Leaving the card that was pending → cancel the pending open.
      if (state.pendingId === event.id) {
        const next = { ...state, pendingId: null, pendingSince: null };
        // If a preview is open for this same card, start the close grace.
        if (state.openId === event.id) {
          return { state: { ...next, closingSince: now }, armInMs: config.closeDelayMs };
        }
        return { state: next, armInMs: null };
      }
      // Leaving the open card (without a pending) → start close grace.
      if (state.openId === event.id) {
        return { state: { ...state, closingSince: now }, armInMs: config.closeDelayMs };
      }
      return { state, armInMs: null };
    }

    case "enterPreview": {
      // Pointer entered the preview panel → cancel any pending close.
      return { state: { ...state, closingSince: null }, armInMs: null };
    }

    case "leavePreview": {
      if (state.openId === null) return { state, armInMs: null };
      return { state: { ...state, closingSince: now }, armInMs: config.closeDelayMs };
    }

    case "tick": {
      let next = state;
      let arm: number | null = null;

      // Promote a pending hover to open once the open delay has elapsed.
      if (next.pendingId !== null && next.pendingSince !== null) {
        const elapsed = now - next.pendingSince;
        if (elapsed >= config.openDelayMs) {
          next = { ...next, openId: next.pendingId, pendingId: null, pendingSince: null, closingSince: null };
        } else {
          arm = config.openDelayMs - elapsed; // re-arm for the remainder
        }
      }

      // Complete a close once the close grace has elapsed.
      if (next.closingSince !== null) {
        const elapsed = now - next.closingSince;
        if (elapsed >= config.closeDelayMs) {
          next = { ...next, openId: null, closingSince: null };
        } else {
          arm = arm === null ? config.closeDelayMs - elapsed : Math.min(arm, config.closeDelayMs - elapsed);
        }
      }

      return { state: next, armInMs: arm };
    }

    case "dismiss":
      return { state: initialPreviewState(), armInMs: null };
  }
}

/** Is a given card's preview currently open? */
export function isPreviewOpen(state: PreviewState, id: string): boolean {
  return state.openId === id && state.closingSince === null;
}
