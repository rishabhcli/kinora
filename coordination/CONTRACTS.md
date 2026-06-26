# CONTRACTS

> Append-only, agent-scoped. Agent 12 reconciles at integration.

## Agent 10 — Reading-room slot contract + open-state machine

The reading room is a **shell** (`apps/desktop/src/reading/`) that composes three
producer components behind stable slots. It is **fully functional on its own**
(built-in stand-ins live in `reading/builtin/`); at integration Agent 12 swaps
the three imports in `reading/producers.tsx` to the real components — same props.

### Entry — `<ReadingRoom>` (rendered by Agent 4's `HomePage`)
```tsx
<ReadingRoom
  book={Book | null}            // null = closed; a Book = open this book
  onClose={() => void}
  originRect={DOMRect | null}   // OPTIONAL — tapped cover's on-shelf rect for the
                                //   open lift. Omit → animate from center.
/>
```

### Open-state machine (`reading/machine.ts`, pure + unit-tested)
```
idle → opening(anim) → loading(meta/pages/shots) → warming(session+first frame)
     → ready → reading → closing → idle
```
- `opening` and the data load run in **parallel**.
- The film is revealed only when `canReveal(state)` — i.e. the open animation is
  ready **AND** the first frame is paintable (real decoded frame OR poster /
  keyframe). Guarantees no flash-of-empty-video.
- Failures never dead-end: a `FALLBACK` event pivots `mode` to the bundled film
  (`fallback.ts`), which always plays. `mode: "unknown" | "live" | "fallback"`.
- Events: `OPEN, META, PAGES, SHOTS, SESSION, FIRST_FRAME, ANIM_READY, FALLBACK(msg?), REVEAL, CLOSE, CLOSED`.
- Selectors: `filmReady(s)`, `canReveal(s)`.

### Slot — Agent 2 `<ScrollFilmEngine>` (`src/reading/ScrollFilmEngine.tsx`)
Owns: the vertical film (crossfade between shot clips — never cut to black), the
scrolling text column, and scroll→focus-word→`api.postIntent`/`api.seek` wiring.
```tsx
<ScrollFilmEngine
  book={Book}
  pages={PageText[]}                 // PageText = { n: number; text: string }
  shots={ShotResponse[]}             // backend shots (may be empty)
  sessionId={string | null}          // live session id, or null on the fallback path
  clipByShot={Record<string,string>} // shot_id → browser-ready clip URL (grows via SSE)
  fallbackFilm={string}              // bundled mp4 when not live / clip missing
  live={boolean}
  prefs={ReadingPrefs}               // from lib/readingPrefs
  reduce={boolean}                   // prefers-reduced-motion
  onProgress={(frac: number, focusWord: number) => void}
  onFirstFrame={() => void}          // first paintable frame → machine FIRST_FRAME
/>
```

### Slot — Agent 6 `<ReadingControls>` (`src/reading/ReadingControls.tsx`)
Self-contained; reads/writes `useReadingPrefs()` (`lib/readingPrefs`). Mounted in
the top bar. Consumes Agent 6 focus/announce utils where available.
```tsx
<ReadingControls />
```

### Wrapper — Agent 4 `<BookOpenTransition>` (`src/motion/BookOpenTransition.tsx`)
```tsx
<BookOpenTransition
  originRect={DOMRect | null}
  cover={{ image?: string; gradient?: string }}
  reduce={boolean}
  onOpened={() => void}   // anim reached reveal point → machine ANIM_READY
  onClosed={() => void}   // close anim finished → machine CLOSED
>
  {children}              // revealed reading-room content
</BookOpenTransition>
```

### Data the loader hands down (`reading/useFilmSession.ts`)
Loads `meta → pages(≤60) → shots` (sorted by word range), then `createSession` +
`openSessionEvents` (SSE) + `postIntent(0)` to prime the scheduler. Maintains:
`pages`, `shots`, `clipByShot`, `bufferAhead`, `bursting`, `inflight`, `zone`,
`crew` (recent `agent_activity`), `live`, `sessionId`. Tears everything down
(SSE close, session release) on unmount / close. SSE auto-reconnects (EventSource).
