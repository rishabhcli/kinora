import { create } from "zustand";

import type { SessionMode } from "../api/types";

// Lightweight, UI-facing reading-session state. The authoritative playhead
// lives in the SyncEngine; this store holds the session identity + the mode
// toggle + the latest reading scalars so panels (metrics, buffer, header) can
// read them without subscribing to the engine directly.
interface SessionState {
  sessionId: string | null;
  bookId: string | null;
  mode: SessionMode;
  focusWord: number;
  velocity: number;
  committedSecondsAhead: number;
  setSession: (sessionId: string | null, bookId: string | null) => void;
  setMode: (mode: SessionMode) => void;
  setReading: (focusWord: number, velocity: number) => void;
  setCommitted: (seconds: number) => void;
  reset: () => void;
}

const initialState = {
  sessionId: null as string | null,
  bookId: null as string | null,
  mode: "viewer" as SessionMode,
  focusWord: 0,
  velocity: 4,
  committedSecondsAhead: 0,
};

export const useSessionStore = create<SessionState>((set) => ({
  ...initialState,
  setSession: (sessionId, bookId) => set({ sessionId, bookId }),
  setMode: (mode) => set({ mode }),
  setReading: (focusWord, velocity) => set({ focusWord, velocity }),
  setCommitted: (seconds) => set({ committedSecondsAhead: seconds }),
  reset: () => set({ ...initialState }),
}));
