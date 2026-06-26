# Coordination — published contracts

Append-only. Each agent documents the surface it PUBLISHES so the Integration
Captain (Agent 12) can wire shells to engines without reading every file.

---

## Agent 02 — Scroll Film Engine

**Module:** `apps/desktop/src/reading/` — `ScrollFilmEngine.tsx`, `FilmPane.tsx`,
`useScrollFilm.ts`, `timeline.ts`.

**Published component** (mounted by Agent 12 inside the `ReadingRoom` shell, where
the old two-pane film+text region was):

```ts
import { ScrollFilmEngine } from "@/reading/ScrollFilmEngine";
import type { ReadingPrefs, ReadingTheme } from "@/lib/readingPrefs";

interface ScrollFilmEngineProps {
  book: Book;                       // from data/books — cover/poster + id
  pages: { n: number; text: string }[]; // ordered page text (empty ⇒ placeholder copy)
  shots: ShotResponse[];            // from api.getShots — source_span.word_range drives the sync map
  clips?: Record<string, string>;   // shot_id → browser-ready mp4 url (live SSE clip_ready)
  sessionId?: string | null;        // present ⇒ scheduler signalling (postIntent/seek) is live
  live?: boolean;                    // false ⇒ bundled fallback film path (WS3)
  fallbackFilm?: string;             // bundled mp4 for the no-backend path (default chosen from book.id)
  prefs: ReadingPrefs;               // theme + typography, from lib/readingPrefs useReadingPrefs()
  effectiveTheme?: ReadingTheme;     // resolved theme (autoNight) from useReadingPrefs(); defaults to prefs.theme
  reducedMotion?: boolean;           // default: framer-motion useReducedMotion(). Agent 6 → useReducedMotionPref()
  bufferAhead?: number | null;       // committed seconds ahead (SSE buffer_state) → progress rail lead
  bursting?: boolean;                // SSE buffer_state.bursting → rail colour
  onProgress?: (fraction: number, focusWord: number) => void; // throttled; for persistence + chrome
}

function ScrollFilmEngine(props: ScrollFilmEngineProps): JSX.Element;
```

Wiring from `ReadingRoom` (Agent 12): pass `useReadingPrefs()`'s `prefs` +
`effectiveTheme`, the SSE-maintained `clipByShot` as `clips`, and the session's
`bufferAhead`/`bursting`. The shell creates the session + owns the SSE stream; the
engine only **signals** the scheduler (seek/postIntent) from scroll.

The engine **owns** the scrollable reading area: the pinned vertical film pane
(720×1280 / 9:16), the scrolling text column, the progress+buffer rail, scroll↔film
sync, scrubbing, parallax, and the scrub indicator. The `ReadingRoom` shell keeps
the top bar, appearance popover, cover-open animation, backdrop, and Escape handling.

**Consumes:** `api.getShots`/`createSession`/`postIntent`/`seek`/`openSessionEvents`
+ `toBrowserUrl` (Agent 12, `lib/api.ts`); stitched event-film API + sync map when
it lands (Agent 1/3) — adapter point is `useScrollFilm`'s timeline build; design
tokens (Agent 10); `useReducedMotionPref()` + `src/motion/` primitives (Agent 6),
passed in via `reducedMotion` rather than hard-imported.

**Tests:** `pnpm --filter @kinora/desktop test:reading` (pure timeline math).
