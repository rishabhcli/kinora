# Cross-seam requests — Agent 10 (reading-room shell)

Agent 12 applies these centrally (shared seams: `lib/api.ts`, the `ReadingRoom`
re-export shim, `main.tsx`, `package.json`).

## 1. `apps/desktop/src/components/ReadingRoom.tsx` → re-export shim
Per mission, the implementation moves to `apps/desktop/src/reading/ReadingRoom.tsx`.
I leave `components/ReadingRoom.tsx` as a one-line re-export so `HomePage.tsx`
(Agent 4) keeps resolving `import ReadingRoom from "./ReadingRoom"` unchanged:

```ts
export { default } from "../reading/ReadingRoom";
```

Status: **done on this branch** (shim committed). Flagging because the shim file
is on the shared-seam list — verify no conflict with Agent 4's `HomePage.tsx`.

## 2. Wire the real producer components (integration swap)
`apps/desktop/src/reading/producers.tsx` exports built-in stand-ins so the room is
fully functional standalone. At integration, swap each to the real component. I
diffed the real contracts now on `overnight/integration` against my slots — they
are CLOSE but need small adapters (do these in `producers.tsx` only):

- **ScrollFilmEngine** (Agent 2 `src/reading/ScrollFilmEngine.tsx`): real prop is
  `reducedMotion?` (mine passes `reduce`) and it has **no `onFirstFrame`**. Adapter:
  map `reduce`→`reducedMotion`; since there's no onFirstFrame, rely on the shell's
  warming safety-timeout (already dispatches FIRST_FRAME after 2.6s) — or ask Agent 2
  to add `onFirstFrame?()` for a frame-accurate reveal. `sessionId/live/fallbackFilm`
  are optional there; my shell always passes them (fine).
- **ReadingControls** (Agent 6 `src/reading/ReadingControls.tsx`): real props
  `{ prefs, onChange, voices? }` — drop my `reduce` (it reads reduced-motion itself).
  Compatible.
- **BookOpenTransition** (Agent 4 `src/motion/BookOpenTransition.tsx`): real shape
  differs most — `{ open: boolean, originRect: Rect (required), cover: CoverArt,
  onOpened?, onClosed?, children: (opened: boolean) => ReactNode }` (render-prop
  children; no `reduce`; uses `useMotion()`). Per its own header it does the
  shelf→center TRAVEL and hands the REVEAL to the room. Two options:
  (a) keep my `BuiltinBookOpenTransition` for the hinge-open reveal and let Agent 4's
      component wrap the shell at the HomePage level for the shelf travel; or
  (b) adapt in `producers.tsx`: pass `open={true}`, thread `originRect` (now optional
      on my `<ReadingRoom>`), wrap my children as `() => children`, drop `reduce`.
  Recommend (a) — they're complementary (travel vs reveal), matching Agent 4's note.

## 2b. `useReadingPrefs` moved to `a11y/readingPrefs.ts` (Agent 6)
On integration, prefs live at `src/a11y/readingPrefs.ts` (my branch still imports
`../lib/readingPrefs`). At merge, repoint imports in `reading/builtin/BuiltinFilm.tsx`,
`reading/builtin/BuiltinControls.tsx`, `reading/ReadingRoomShell.tsx` to the a11y path
(or keep a `lib/readingPrefs` re-export). Same `ReadingPrefs`/`READING_THEMES`/
`READING_SPACINGS`/`clampPref` API, so a path change is all that's needed.

## 3. (Optional, CI parity) Add vitest to `apps/desktop`
Pure logic is currently TDD'd with Node 26 native type-stripping + `node --test`
(no dep needed; runs in this repo today). For monorepo CI parity, optionally add:

```jsonc
// apps/desktop/package.json
"devDependencies": { "vitest": "^2.1.0" },
"scripts": { "test": "vitest run" }
```

My `*.test.ts` files use only `node:test` + `node:assert/strict`, so they keep
working with or without vitest. Not required for the DoD (`typecheck && build`).
