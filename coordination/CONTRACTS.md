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
