# CONTRACTS REGISTRY

Contracts are defined **statically up front** so agents never negotiate at runtime —
code against the contract and **stub if a producer hasn't merged yet.**

**Rules:** Contracts are **append-only and stable once published**. A breaking change
to a published contract must be announced in `STATUS.md` and coordinated with consumers
via the request queues. Each producer **finalizes their own section** (marks it
`STATUS: FINAL` + the exact exported signatures) when their branch is ready to merge.

---

## 1. Design tokens — Producer **A8** → all
Semantic CSS vars + Tailwind classes:
- `--k-bg`, `--k-surface`, `--k-text`, `--k-text-muted`, `--k-accent`
- elevation `--k-elev-1..4`
- fonts `--k-font-ui`, `--k-font-reading`, `--k-font-display`
- theme sets (light / dark / sepia / etc.)

**Rule: no raw hex outside `tokens.css`.** Legacy `kinora-*` Tailwind colors kept as **aliases**.
Lives in `src/styles/tokens.css` (+ `base.css`, `glass.css`). `tailwind.config.js` maps the vars.

> STATUS: SEEDED — awaiting A8 finalization.

## 2. Motion — Producer **A4** → all
`src/motion/` exports (all reduced-motion-aware, gate on A6's `useReducedMotionPref()`):
- `<Reveal>`, `<PageTransition>`, `<BookOpenTransition>`, `<ShelfScroller>`, `<Tilt>` / `useTilt`
- `springs` (named spring presets)

CSS side: `src/styles/motion.css`. Built on `framer-motion@12`.

> STATUS: SEEDED — awaiting A4 finalization.

## 3. A11y — Producer **A6** → all
`src/a11y/` exports:
- `useReducedMotionPref()`, `useReadingPrefs()`
- `<ReadingControls>` (also lives at `reading/ReadingControls.tsx` per ownership)
- `announce(msg, politeness?)`, `<VisuallyHidden>`
- `trapFocus(el)` / `restoreFocus()`, `registerShortcut(combo, handler)`
- `a11y-checklist.md` — every change must meet it.

CSS side: `src/styles/a11y.css`. `lib/readingPrefs.ts` → `a11y/readingPrefs.ts` (shim kept by Captain).

> STATUS: SEEDED — awaiting A6 finalization.

## 4. Icons — Producer **A9** → all
`<Icon name weight size mode title />` + `IconName` union (TS) + `components/icons/migration-map.md`.
Owners swap inline SVG → `<Icon>` **in their own files**; A9 does the final sweep through the Captain.

> STATUS: SEEDED — awaiting A9 finalization.

## 5. Reading-room slots — Producer **A10** → A2 / A4 / A6
`<ReadingRoom book onClose>`; mounts:
- A2's `<ScrollFilmEngine>`
- A6's `<ReadingControls>`
- wrapped by A4's `<BookOpenTransition>`

Publishes the **open-state machine** + **projects state on open**.

> STATUS: SEEDED — awaiting A10 finalization.

## 6. Event film — Producer **A1** → exposed by **A3** → consumed by **A2**
Stitched mp4 url + **sync map**:
```ts
type SyncEntry = {
  shot_id: string;
  scene_id: string;
  word_range: [number, number];   // [start, end] word indices into source span
  t_start_s: number;
  t_end_s: number;
};
```
Vertical **720×1280**. `KINORA_LIVE_VIDEO` stays OFF — fallback is the Ken-Burns mp4 ladder.

> STATUS: SEEDED — awaiting A1/A3 finalization.

## 7. API client — Producer **A12** → A3 / A5 / A10  ✅ PUBLISHED
From `apps/desktop/src/lib/api.ts`:
- `export const BASE: string` — API base URL (env `VITE_KINORA_API_URL` or `http://localhost:8000`).
- `export const auth` — token store: `auth.get()`, `auth.set(token)`, `auth.clear()`.
- `export async function http<T>(path: string, init?: RequestInit): Promise<T>` — fetch wrapper
  that prefixes `BASE`, attaches the bearer token, sets JSON headers, throws `ApiError` on non-2xx,
  and returns parsed JSON (or `undefined` for 204).
- `export function toBrowserUrl(url: string): string` — rewrites `minio:9000` → `localhost:9000`.

Feature methods live in **`src/lib/api/*.ts`** and `import { http, BASE, auth, toBrowserUrl } from '../api'`:
- A3 → `src/lib/api/films.ts`
- A5 → `src/lib/api/library.ts`

> STATUS: **FINAL** (Captain owns `api.ts`; signatures above are stable). See MERGE-LOG for the refactor commit.

## 8. Cover fields — Producer **A5** → A10 / A11
- `cover_url` / `cover_key` on `Book` / `BookResponse`.
- `GET /api/books/{id}/cover`.

> STATUS: SEEDED — awaiting A5 finalization.

---

## Backend route registration (Captain wires on merge)
New routers are registered by **A12** when the producing branch merges:
- `routes/films.py` (A3), `routes/library.py` (A5), `routes/metrics.py` (A7).
Producers ship the `APIRouter`; the Captain adds the `include_router(...)` call.

## Alembic migration ordering (Captain assigns `down_revision`)
Multiple branches add migrations (A5 cover field, A7 indexes). To avoid two heads, the
Captain assigns sequential `down_revision`s **at merge time**. Producers leave a clear
`# down_revision: <TBD by Captain>` marker and the upgrade/downgrade bodies.

---

## Context7 library ids (use the SAME doc sources fleet-wide)
Before implementing against an external API, pull current docs via Context7
(`resolve-library-id` → `query-docs`) — training data is stale for much of this stack.

| Library / API | Use for | Resolve hint |
|---|---|---|
| framer-motion (v12) | A4 motion, A10 transitions | `framer-motion` / `motion` |
| FastAPI | A1/A3/A5/A7 routes, DI | `fastapi` |
| React 18 | all renderer agents | `react` |
| Vite | A7 `vite.config.ts`, build | `vite` |
| Tailwind CSS | A8 tokens/config | `tailwindcss` |
| Electron (33) | window/native seams | `electron` |
| SQLAlchemy / Alembic | A5/A7 migrations | `sqlalchemy`, `alembic` |
| DashScope / Wan / Qwen | A1 render (do NOT guess model ids) | search "dashscope" / see backend/.env |

**Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron
APIs from memory — verify with Context7 first. Working Wan ids are in `backend/.env`
(see root `CLAUDE.md`); placeholder ids like `wan2.7-t2v` are invalid.


---

# Producer-finalized contract detail (appended on merge)

_Below: each producer's full published interface, folded in by the Captain at merge time._

## Agent 08 — DESIGN TOKENS (color · depth · typography)

**Source of truth:** `apps/desktop/src/styles/tokens.css` (the `--k-*` custom
properties) mirrored into Tailwind via `apps/desktop/tailwind.config.js`.

### THE RULE (fleet-wide)
**No agent writes raw hex/rgb outside `tokens.css`.** Everyone consumes tokens —
either the Tailwind classes below or the `var(--k-*)` custom properties in plain
CSS. This is what lets the whole app re-skin from one file. New surfaces must use
a semantic token, never a literal colour.

### Color tokens
Stored as **space-separated RGB triples** (`--k-*-rgb`) so Tailwind `/<alpha>`
opacity modifiers work, plus `--k-*` convenience solids for plain CSS.

| Semantic | CSS var (solid) | RGB triple var | Tailwind class | Notes |
|---|---|---|---|---|
| App canvas | `--k-bg` | `--k-bg-rgb` | `bg-bg` / `bg-kinora-bg` | warm graphite |
| Deepest bg | `--k-bg-deep` | `--k-bg-deep-rgb` | `bg-bg-deep` / `bg-kinora-bg-deep` | behind everything |
| Surface | `--k-surface` | `--k-surface-rgb` | `bg-surface` / `bg-kinora-surface` | resting panels/cards |
| Surface raised | `--k-surface-raised` | `--k-surface-raised-rgb` | `bg-surface-raised` | popovers/raised |
| Surface high | `--k-surface-high` | `--k-surface-high-rgb` | `bg-surface-high` | menus/sheets |
| Text primary | `--k-text` | `--k-text-rgb` | `text-text` / `text-kinora-text` | 15.1:1 on bg |
| Text muted | `--k-text-muted` | `--k-muted-rgb` | `text-muted` / `text-kinora-muted` | 7.8:1 on bg |
| Text subtle | `--k-text-subtle` | `--k-subtle-rgb` | `text-subtle` / `text-kinora-subtle` | 5.1:1 on bg |
| Faint (deco) | — | `--k-faint-rgb` | `text-faint` | NOT for text |
| Accent | `--k-accent` | `--k-accent-rgb` | `text-accent` / `*-kinora-gold` | "lumen" gold |
| Accent strong | `--k-accent-strong` | `--k-accent-strong-rgb` | `*-accent-strong` / `*-kinora-gold-light` | bright glint |
| Accent deep | `--k-accent-deep` | `--k-accent-deep-rgb` | `*-accent-deep` | ember fills |
| Accent cool | `--k-accent-cool` | `--k-accent-cool-rgb` | `*-accent-cool` | cinema-teal |
| Success | — | `--k-success-rgb` | `*-success` | |
| Warning | — | `--k-warning-rgb` | `*-warning` | |
| Danger | — | `--k-danger-rgb` | `*-danger` | |
| Info | — | `--k-info-rgb` | `*-info` | |
| Hairline | `--k-border` | — | `border-hairline` | text @ 10% |
| Hairline strong | `--k-border-strong` | — | `border-hairline-strong` | text @ 18% |

Material fills: `--k-surface-glass`, `--k-surface-glass-strong`, `--k-scrim`,
`--k-specular`, `--k-specular-soft`.

**Legacy aliases kept working** (no broken styles): every `*-kinora-bg`,
`*-kinora-bg-deep`, `*-kinora-text`, `*-kinora-muted`, `*-kinora-subtle`,
`*-kinora-gold`, `*-kinora-gold-light` (including opacity modifiers like
`text-kinora-text/85`, `bg-kinora-gold/50`) resolves to the new tokens.

### Depth / material
- Elevation ladder: `--k-elev-1` … `--k-elev-5` → Tailwind `shadow-elev-1..5`.
  Plus `--k-ring-top` (specular rim), `--k-ring-edge`, `--k-glow-accent` →
  `shadow-ring-top`, `shadow-glow`.
- Blur: `--k-blur-sm|-|-lg|-xl` → `backdrop-blur-k-sm|-k|-k-lg|-k-xl`; `--k-saturate`.
- Material classes (in `glass.css`): **`.surface`**, **`.surface-raised`**,
  **`.surface-frosted`** (the new primitives — prefer these), plus token-driven
  re-skins of the existing `.kinora-bg`, `.glass-card`, `.glass-input`,
  `.liquid-glass-dock`, `.footer-glass`. Frosted material degrades to solid under
  `prefers-reduced-transparency`. **Never call it Liquid Glass** (native shell only).

### Typography
- Faces: `--k-font-ui` (system-first / SF Pro), `--k-font-display` (Fraunces),
  `--k-font-reading` (Newsreader), `--k-font-mono`. Tailwind: `font-ui`,
  `font-display`, `font-reading`, `font-mono`; `font-sans`→UI, `font-serif`→display.
- Scale: `--k-text-xs … --k-text-5xl` → `text-k-xs … text-k-5xl`.
- Weights `--k-weight-*`; leading `--k-leading-*`; tracking `--k-tracking-*`
  (→ `tracking-k-display|-k-tight|-k-wide|-k-caps`); measure `--k-measure(-narrow|-wide)`.
- Helpers (base.css): `.font-display`, `.font-reading`, `.text-eyebrow`,
  `.prose-reading`, `.nums-tabular`, `.scrollbar-slim`.

### Theme sets (Agent 06 binds these to the reading pane / a11y toggle)
Reading themes are values, not a global app swap — supplied as tokens:
`--k-read-dark-*`, `--k-read-night-*`, `--k-read-sepia-*`, `--k-read-paper-*`,
`--k-read-contrast-*` (each `-bg`, `-ink` as RGB triple, `-swatch`). High-contrast
overrides bind via `[data-contrast="high"]` on `<html>`. See
`requests/agent-06-from-08.md` for the recommended `READING_THEMES` values.

### What Agent 08 consumes
Nothing. (Coordinates AA / a11y with Agent 06; coordinates the `index.css` split +
font/dep tooling with Agent 12 — see `requests/agent-12-from-08.md`.)


## Agent 06 — A11Y (folded in on merge)

## Agent 06 — Accessibility (`apps/desktop/src/a11y/`)

> Status: **WS1–WS3 LANDED** on `agent/06-a11y` (foundation + ReadingControls +
> read-aloud, 90 tests, typecheck + build green). Import from `@/a11y/*`
> (alias `@` → `apps/desktop/src`). Signatures below are the contract.

### Reduced motion — the single source of truth

```ts
// apps/desktop/src/a11y/useReducedMotionPref.ts
function useReducedMotionPref(): boolean;       // OS `prefers-reduced-motion` OR in-app override
function setReducedMotionOverride(v: boolean | null): void;  // null = follow OS
function getReducedMotionSnapshot(): boolean;   // non-hook read (for imperative code)
```

**Agent 4 (motion) and every animating component must consume `useReducedMotionPref()`**
instead of framer-motion's `useReducedMotion()` directly, so the in-app toggle works.

### Reading preferences (moved here from `lib/readingPrefs.ts`; shim left behind)

```ts
// apps/desktop/src/a11y/readingPrefs.ts  (re-exported from lib/readingPrefs.ts)
function useReadingPrefs(): {
  prefs: ReadingPrefs;
  update: (p: Partial<ReadingPrefs>) => void;
  effectiveTheme: ReadingTheme;     // honours autoNight
};
interface ReadingPrefs {
  theme: ReadingTheme;              // 'dark' | 'night' | 'sepia' | 'paper'
  autoNight: boolean;
  fontFamily: ReadingFontFamily;    // 'sans' | 'serif' | 'dyslexic'
  fontScale: number;                // 0.8–1.6 of 15px base
  leading: number;                  // line-height 1.3–2.4
  measure: number;                  // ch, 44–88
  spacing: ReadingSpacing;          // 'normal' | 'relaxed' | 'loose'
  brightness: number;               // 0.5–1.0 page dim
  readingMode: 'scroll' | 'paged';
  ttsRate: number;                  // 0.5–2.0
  ttsVoiceURI: string | null;       // null = system default
}
```

### Announcer / focus / keyboard / hidden-text primitives

```ts
// apps/desktop/src/a11y/announce.ts
function announce(message: string, politeness?: 'polite' | 'assertive'): void;

// apps/desktop/src/a11y/focus.ts
function trapFocus(container: HTMLElement): () => void;  // returns release()
function restoreFocus(previouslyFocused: HTMLElement | null): void;
function getFocusable(container: HTMLElement): HTMLElement[];

// apps/desktop/src/a11y/keyboard.ts
function registerShortcut(
  combo: string,                     // e.g. 'mod+,'  'shift+?'  'r'
  handler: (e: KeyboardEvent) => void,
  opts?: { scope?: string; description?: string; whenInputFocused?: boolean },
): () => void;                       // returns unregister()
```

```tsx
// apps/desktop/src/a11y/VisuallyHidden.tsx
<VisuallyHidden as="span">screen-reader-only text</VisuallyHidden>
```

### Reading controls panel (Agent 10 mounts via the reading-room slot)

```tsx
// apps/desktop/src/reading/ReadingControls.tsx
<ReadingControls
  prefs={ReadingPrefs}
  onChange={(p: Partial<ReadingPrefs>) => void}
  voices?={SpeechSynthesisVoice[]}   // optional; component will enumerate if omitted
/>
```

### Read-aloud engine + view (Web Speech API) — **LANDED** `cc117b7`

```ts
// apps/desktop/src/a11y/tts.ts
function tokenizeWords(text: string): TtsToken[];                 // pure, char offsets
function findTokenAtChar(tokens: TtsToken[], charIndex: number): TtsToken | null;
function useTts(opts: {
  getText: () => string;
  rate?: number; voiceURI?: string | null;
  onError?: (e: string) => void;
  onActiveWordChange?: (token: TtsToken | null) => void;
}): {
  supported: boolean;
  state: "idle" | "playing" | "paused";
  activeWordIndex: number;           // -1 when none
  tokens: TtsToken[];
  play(): void; pause(): void; resume(): void; toggle(): void; stop(): void;
};
interface TtsToken { text: string; start: number; end: number; index: number }
```

```tsx
// apps/desktop/src/a11y/ReadAloudView.tsx — mount inside the text-pane (Agent 10)
<ReadAloudView text={pageText} rate? voiceURI? showControls? />
// renders words + highlights the spoken one (aria-current) in lockstep.
```

### Checklist every agent must satisfy

See `apps/desktop/src/a11y/a11y-checklist.md` — labels, roles, focus order,
contrast, keyboard, reduced-motion. Linked from each `coordination/requests/agent-XX.md`.

---


## Agent 07 — OPTIM (folded in on merge)

## Agent 07 — Optimization (perf helpers, cost meter, config flags)

### Backend — `app/optim/` (NEW package, additive)

**`cost_meter.py`**
- `Price` (frozen dataclass): per-model unit prices — `input_per_1k`, `output_per_1k` (tokens), `per_image`, `per_audio_second`, `per_video_second` (all USD, `Decimal`).
- `PRICING: dict[str, Price]` — table keyed by model id (Qwen/Wan). Documented "prices as of" date; override via settings.
- `cost_of(usage: Usage, pricing: Mapping[str, Price] = PRICING) -> Decimal` — pure: USD cost of one `providers.types.Usage`. Unknown model → `Decimal(0)` + structured warn (never raises in a hot path).
- `CostMeter` — implements `UsageSink` (`__call__(usage: Usage) -> None`). Rolls up `{total, by_model, by_operation, by_book, by_session}`. Attribution via `cost_context(...)`.
- `cost_context(*, book_id=None, session_id=None)` — contextmanager setting a `ContextVar` the meter reads for per-book/per-session attribution. No-op safe when unset.
- Wires via `create_providers(usage_sink=CostMeter(...))` at the `Container.providers` seam (proposed to Agent 12 — see requests).

**`routing.py`**
- `ModelRouter.route(site: str, default_model: str) -> str` — returns the cheapest model that holds the quality bar for a call-site; **default table returns `default_model` unchanged** (zero behavior change until an override is enabled). Per-site overrides + a quality guard.
- Call-site keys: `"showrunner" | "continuity" | "adapter" | "cinematographer" | "critic" | "comment_classifier"`.

**`prompt_compress.py`** — pure helpers: `estimate_tokens(text)`, `dedupe_canon(blocks)`, `trim_context(messages, budget_tokens)`, `compact_json_schema(...)`. Cut input tokens + retries; behavior-preserving.

**`batch.py`** — `gather_bounded(coros, *, limit)`, `with_backoff(fn, *, retries, on=RateQuota)` — bounded concurrency + clean `429 Throttling.RateQuota` backoff.

### Backend — new config flags (default-safe; proposed to Agent 12 for `config.py`)
- `optim_cost_meter_enabled: bool = False` — attach the `CostMeter` usage sink.
- `optim_routing_enabled: bool = False` — let `ModelRouter` overrides take effect (off ⇒ current models).
- `optim_cache_enabled: bool = False` — enable content-hash memoization of deterministic agent outputs.
- (Pricing override) `optim_pricing_json: str | None = None` — optional JSON to override `PRICING`.

### Backend — new route (additive; Agent 12 registers the include)
- `app/api/routes/optim.py` → `GET /api/optim/cost` (per-book/session rollup JSON) + `GET /api/optim/perf` (latency/queue snapshot). **Named `optim.py`, not `metrics.py`** — `metrics.py` is already the `/eval` route.

### Client — `apps/desktop/src/lib/perf.ts` (NEW, opt-in helpers)
- `lazyImport<T>(factory: () => Promise<{default: T}>): LazyExoticComponent` — `React.lazy` + retry-on-chunk-error wrapper.
- `preloadVideo(url: string, opts?): void` — prefetch + warm the HTTP cache for an upcoming clip.
- `decodeOnIdle(img: HTMLImageElement): Promise<void>` — `requestIdleCallback`-gated `img.decode()`.
- `mark(name)`, `measure(name, startMark)` — thin `performance.*` wrappers for TTI / decode marks.

_Adopt opt-in; none of these change behavior unless a component imports them._

---

<!-- Other agents: append your section below. -->


## Agent 10 — READING-ROOM (folded in on merge)

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
**Controlled** — the shell owns the single `useReadingPrefs()` instance and passes
`prefs` + `onChange` down to BOTH the controls and the film engine, so a theme/size
change reflects live in the text. (Separate `useReadingPrefs()` instances would NOT
sync, since it is local state.) Mounted in the top bar.
```tsx
<ReadingControls prefs={ReadingPrefs} onChange={(p: Partial<ReadingPrefs>) => void} reduce={boolean} />
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


## Agent 02 — SCROLL-FILM (folded in on merge)

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


## Agent 03 — FILM API (folded in on merge)

## Agent 03 — Film API, Sync Map & Client Wiring

**Status:** PUBLISHED (v1). Authoritative for the event/scene **film** HTTP boundary, the
**sync map** shape, and the `event_stitched` / `scene_stitched` SSE payloads.

**Owns (publishes):** `backend/app/api/routes/films.py`, `backend/app/films/contract.py`
(pure wire models + builders), `apps/desktop/src/lib/api/films.ts` (TS types + methods).

**Consumes:** Agent 1's stitch output (`app.render.stitch.StitchResult` /
`app.render.sync_map.SyncSegment`) and the object-store keys (`app.storage.object_store.keys`).

### 0. Terminology — "event" ≡ "scene" (1:1 today, forward-compatible)

Kinora's persistent stitch unit is the **scene** (`scenes` table, ordered by `scene_index`;
the §9.6 stitch boundary). There is **no `events` table** and **no event-level video** in the
data model. This API surfaces each scene as the reader-facing **event film** (one continuous
film a reader watches), so:

- `event_id == scene_id`, `event_index == scene_index` **today**.
- `EventFilm.scenes[]` lists the scene(s) composing the event — today exactly one (itself).
- A future Agent-1 event-grouping (several scenes → one event) extends `scenes[]` and the
  event-level `sync_map` **without breaking this contract**.

### 1. Wire conventions

- **JSON is snake_case** (matches existing `ShotResponse`, `BookResponse`, `oss_url`).
- **`word_range` is `[start, end]` inclusive** in global word-index space (matches
  `app.agents.contracts.SourceSpan.word_range` and the §4.2 source-span index).
- **Timeline seconds** (`t_start_s`, `t_end_s`, `page_turn_at_s`, word `t_start`/`t_end`)
  are on the **film timeline** (scene/event timeline after cumulative merge), not per-shot.
- `t_start_s` / `t_end_s` are the canonical names for what `app.render.sync_map.SyncSegment`
  calls `video_start_s` / `video_end_s`. **`t_start_s ≡ video_start_s`, `t_end_s ≡ video_end_s`.**

### 2. Types (the published contract)

```jsonc
// SyncWord — per-word karaoke timing (§9.4). Timings are film-timeline seconds.
SyncWord = {
  word_index: int,            // global word index; ties to source-span index + page word_boxes
  text: string,
  t_start: float,
  t_end: float,
  bbox: [float, float, float, float] | null   // normalized [x,y,w,h] page box, or null
}

// FilmSyncSegment — one shot's window on the film timeline.
// Core fields {shot_id, scene_id, word_range, t_start_s, t_end_s} are REQUIRED;
// {page, page_turn_at_s, words} are the §9.4 enrichment (page-turn + karaoke).
FilmSyncSegment = {
  shot_id: string,
  scene_id: string,
  word_range: [int, int],     // [start, end] inclusive, global word-index
  t_start_s: float,
  t_end_s: float,
  page: int,
  page_turn_at_s: float,      // when SyncEngine flips the PDF (slightly before t_end_s)
  words: SyncWord[]
}

// FilmSyncMap — the ordered segments for one film (scene or event).
FilmSyncMap = {
  scene_id: string,           // the scene/event id this map belongs to (== event_id at event level)
  duration_s: float,
  segments: FilmSyncSegment[] // ordered by t_start_s (reading order)
}

// SceneRef — lightweight pointer to a composing scene (in EventFilm.scenes[]).
SceneRef = {
  scene_id: string,
  scene_index: int,
  word_range: [int, int],
  stitched: bool,
  duration_s: float | null
}

// SceneFilm — GET /api/books/{book_id}/scenes/{scene_id}/film
SceneFilm = {
  scene_id: string,
  event_id: string,           // == scene_id today
  book_id: string,
  scene_index: int,
  event_index: int,           // == scene_index today
  page_start: int,
  page_end: int,
  word_range: [int, int],     // span covered by the scene's accepted shots
  stitched: bool,             // true iff the stitched mp4 exists in the object store
  oss_url: string | null,     // presigned GET URL for the stitched mp4 (null until stitched)
  url_expires_at: string | null,  // ISO-8601 UTC; null when public (non-expiring) or no film
  duration_s: float | null,
  shot_count: int,            // accepted shots in the film
  sync_map: FilmSyncMap
}

// EventFilm — items in GET /api/books/{book_id}/events
EventFilm = {
  event_id: string,           // == scene_id today
  event_index: int,           // == scene_index today
  book_id: string,
  page_start: int,
  page_end: int,
  word_range: [int, int],
  stitched: bool,
  oss_url: string | null,
  url_expires_at: string | null,
  duration_s: float | null,
  shot_count: int,
  sync_map: FilmSyncMap,      // event-level (== the single scene's map today)
  scenes: SceneRef[]          // composing scenes (today: [the event's own scene])
}

// RestoreState — open-book context for Agent 12 to restore (§5.2). null when no prior session.
RestoreState = {
  session_id: string,
  focus_word: int,                  // last reading position (global word index)
  current_event_index: int | null,  // event (scene) index containing focus_word
  current_scene_id: string | null,
  mode: string                      // "viewer" | "director"
} | null

// EventsResponse — GET /api/books/{book_id}/events
EventsResponse = {
  book_id: string,
  url_ttl_s: int,             // presigned-URL lifetime in seconds (see §4)
  events: EventFilm[],        // ordered by event_index
  restore: RestoreState
}
```

### 3. Endpoints

| Method | Path | Response | Notes |
|---|---|---|---|
| GET | `/api/books/{book_id}/events` | `EventsResponse` | All events (scenes) for a book + restore state. Auth: book owner. |
| GET | `/api/books/{book_id}/scenes/{scene_id}/film` | `SceneFilm` | One scene's film (partial load). Auth: book owner. 404 if scene not in book. |

- Auth: `Authorization: Bearer <jwt>` (same as every route). 404 (`book_not_found` /
  `scene_not_found`) when the book isn't owned by the caller or the scene isn't in the book.
- A film with **no accepted shots yet** returns `stitched:false, oss_url:null` and an empty
  `sync_map.segments` — the endpoint never blocks on rendering (works with `KINORA_LIVE_VIDEO` off).

### 4. Presigned URL lifetime + refresh semantics

- `oss_url` is an **S3/MinIO presigned GET URL** valid for `url_ttl_s` seconds
  (default **3600s**, from `ObjectStore` TTL).
- When `S3_PUBLIC_BASE_URL` is configured (local dev), `oss_url` is a **stable public URL**
  (`{base}/{key}`) that does **not** expire → `url_expires_at` is `null`.
- `url_expires_at` (ISO-8601 UTC) is set for signed URLs so the client knows when to refresh.
- **Refresh pattern:** before `url_expires_at`, re-`GET` the same endpoint to mint fresh URLs.
  For long playback, re-`GET .../scenes/{scene_id}/film`. The `scene_stitched` /
  `event_stitched` SSE frames also carry a fresh `oss_url`.
- The client must rewrite the host for the browser with `toBrowserUrl()` (minio:9000→localhost:9000).

### 5. SSE payloads (WS3) — ride Agent 12's session stream (§5.6)

Event names match §5.6. `sync_map` is a `FilmSyncMap` (canonical shape above).

```jsonc
// scene_stitched — replace per-shot playback with the stitched scene (§9.6)
{ event: "scene_stitched", scene_id: string, oss_url: string, sync_map: FilmSyncMap }

// event_stitched — event-level rollup ready (NEW; event == scene today)
{ event: "event_stitched", event_id: string, oss_url: string, sync_map: FilmSyncMap }
```

Builders live in `backend/app/films/contract.py`. **Exact signatures:**
`scene_stitched_event(*, scene_id, oss_url, sync_map: FilmSyncMap)` and
`event_stitched_event(*, event_id, oss_url, sync_map: FilmSyncMap)`. A producer emits in two
steps — convert the merged render map to a `FilmSyncMap`, then build the frame:

```python
from app.films.contract import film_sync_map_from_merged, scene_stitched_event

# spans: {shot_id: [word_start, word_end]} from each shot's source_span.word_range
fsm = film_sync_map_from_merged(stitched.sync_map, scene_id=stitched.scene_id, spans=spans)
await redis.publish(channel, scene_stitched_event(
    scene_id=stitched.scene_id, oss_url=stitched.clip_url, sync_map=fsm))
```

Emitting via these keeps SSE byte-compatible with REST (no client adapter). **Current state:** the
worker (`app/queue/worker.py`, Agent 1) still emits render-shaped `scene_stitched`
(`video_start_s`/no `word_range`) and nothing emits `event_stitched` yet — so until a producer
adopts the builders the SSE frames do **not** match this `FilmSyncMap` shape on the wire. Wiring
is a cross-seam item in `requests/agent-03.md`.

### 6. Client (films.ts) — Agent 2 consumes this, no adapter

`apps/desktop/src/lib/api/films.ts` exports the TS mirror of every type above plus:

```ts
films.getEvents(bookId: string): Promise<EventsResponse>
films.getSceneFilm(bookId: string, sceneId: string): Promise<SceneFilm>
films.filmUrl(film: { oss_url: string | null }): string   // toBrowserUrl(oss_url) convenience
```

The TS field names/types are identical to the JSON above (snake_case), so the same objects
arriving via SSE (`scene_stitched`/`event_stitched`) and via REST share one type set.


## Agent 05 — LIBRARY (folded in on merge)

## Agent 05 — Library / books / covers

### Backend (consumed by Agent 10 reading, Agent 11 login backdrop)

- `Book.cover_key: str | None` — object-store key of the book's cover image
  (`covers/{book_id}`), set by the seeder / `fetch_hd_covers.py` / EPUB upload.
- `BookResponse.cover_url: str | None` — presigned GET URL for `cover_key`
  (added additively; `null` when a book has no cover yet). Present on every
  `GET /api/books` and `GET /api/books/{id}` response.
- `GET /api/books/{id}/cover` — authed + ownership-checked; **302 redirect** to the
  presigned cover URL (or 404 if the book has no cover). Stable accessor for
  native shells / `<img>` that can resolve via the BookResponse `cover_url`.

### Catalogue manifest

- `assets/books/catalog.json` — array of `{ id, gutenberg_id, title, author,
  genre, era, tags[], cover_source, source }`. The Retrieval/Understanding
  manifest the seeder writes; safe for other agents to read for metadata.

### Desktop client (`apps/desktop/src/lib/api/library.ts`)

- `listLibrary()` → `Book[]` (real backend, cover_url-bearing) — to be published.
- `uploadBook(file, opts)` → upload + ingest-status polling — to be published.

_Statuses/shapes finalize as the work lands; this section is updated per commit._
