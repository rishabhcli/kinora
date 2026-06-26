# Audit findings for Agent 10 (Reading room / book-open) — from Agent 06 (a11y)

Scope: `apps/desktop/src/components/ReadingRoom.tsx` (line refs vs. base `4863a0c`;
they’ll have shifted in your refactor — match by behavior). Meet `a11y-checklist.md`.

### 1. Mount `<ReadingControls>` instead of the inline prefs popover  ★ high value
The "Aa" popover is inlined at `ReadingRoom.tsx:317-355`. Replace it with the new
controlled panel:
```tsx
import { ReadingControls } from "@/reading/ReadingControls";
<ReadingControls prefs={prefs} onChange={update} />   // prefs/update from useReadingPrefs
```
It adds font family (incl. dyslexia), brightness, scroll/paged, read-aloud, and the
a11y display toggles — all keyboard + VoiceOver operable. Keep your `useReadingPrefs`
import working via the `@/lib/readingPrefs` shim (now re-exports `@/a11y/readingPrefs`).

### 2. Trap focus in the reading dialog
The dialog (`:264-266`, role="dialog" aria-modal) saves/restores focus (`:140-167`)
but does **not** trap Tab — focus can leave the modal. Add:
```tsx
import { trapFocus } from "@/a11y/focus";
useEffect(() => { if (!open) return; const release = trapFocus(dialogEl); return release; }, [open]);
```
Also give the prefs popover its own Escape-to-close + focus return (today only the
outer dialog handles Escape).

### 3. Use the one reduced-motion source of truth
`:50` calls framer’s `useReducedMotion()`. Swap to `useReducedMotionPref()` from
`@/a11y/useReducedMotionPref` so the in-app toggle (and high-contrast/transparency)
applies here too.

### 4. Word-synced read-aloud in the text pane  ★ marquee
Reading is paragraph-level today; `WordBox.word_index` (`api.ts:71`) is unused. To get
read-aloud word highlighting, render the page text through the published primitive:
```tsx
import { ReadAloudView } from "@/a11y/ReadAloudView";
<ReadAloudView text={pageText} rate={prefs.ttsRate} voiceURI={prefs.ttsVoiceURI} />
```
It highlights the spoken word in lockstep (proven by 17 tests + the recorded demo in
`artifacts/agent-08/recordings/`). If you want narration-playhead sync instead of TTS
boundaries, request the playhead stream from Agent 1 and drive the same component.

### 5. Landmark + scroll container
Give the reading-room content a `<main id="kinora-main">` (the app skip link targets
it). The scroll container (`:383`) is `tabIndex={0}` with `focus:outline-none`; the new
global `:focus-visible` ring restores a visible focus — don’t re-suppress it.

Verify with `pnpm --filter @kinora/desktop test:a11y` (axe) on the reading room.


---

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
