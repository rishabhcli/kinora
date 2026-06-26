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
