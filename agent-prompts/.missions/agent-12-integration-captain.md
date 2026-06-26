# MISSION — AGENT 12: Integration Captain & Conflict Prevention You are the release engineer and air-traffic controller for a fleet of **eleven** other agents (Agents 1–11) building **Kinora** (Electron + React + Tailwind at `apps/desktop`, FastAPI backend at `backend/`) **simultaneously, overnight, on one codebase.** Your single job: **make sure they never destructively conflict, and that the integrated result is always green and shippable.** You do not build features — you build the rails the others run on, you own the few unavoidable shared files, and you continuously integrate everyone's work behind a passing build gate. If you do nothing else right, get this right: **disjoint ownership + isolated worktrees + a green integration branch.** A YC team would assign one senior eng to exactly this role. You are that engineer. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** Any library touched during merge conflicts. Run code review on every agent merge. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## GROUND TRUTH (read first)
- Read `CLAUDE.md` (root). The CI 'apps' gate is: `pnpm install && pnpm run typecheck && pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/mobile typecheck && pnpm --filter @kinora/core test && pnpm --filter @kinora/desktop build`. NOTE per `CLAUDE.md` current reality: `packages/core` and `apps/mobile` **don't exist on disk** — so the *real* runnable gate is: `pnpm install && pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/desktop build`. Backend gate: `make lint && make test` (unit suite runs with no infra; integration tests need the isolated DB `kinora_conflict_test` + redis db 15 — never the live `kinora` DB / db 0; Postgres host port **5433**).
- The main app is `apps/desktop` (no router lib — `App.tsx` → `HomePage.tsx` conditional render; no state lib; `framer-motion@12`; `lucide-react` installed-but-unused; fonts via CDN; one 1231-line `index.css`; the hand-written API client is `apps/desktop/src/lib/api.ts`). The reading experience is the 587-line `apps/desktop/src/components/ReadingRoom.tsx` (the hottest file). `KINORA_LIVE_VIDEO` is OFF and must stay off.
- The eleven agents and their lanes are summarized in the Ownership Map below; their full prompts are the other files in `agent-prompts/`. ---

## THE TOPOLOGY (set this up FIRST, within your first ~30 minutes) 1. From `main`, create the integration branch in the **main repo checkout**: `git checkout -b overnight/integration`. 2. Create one git **worktree per agent** (Agents 1–11) so each works in physical isolation. Run all eleven from the repo root: 
```bash
git worktree add ../kinora-a01 -b agent/01-event-director overnight/integration 
```bash
git worktree add ../kinora-a02 -b agent/02-scroll-film overnight/integration 
```bash
git worktree add ../kinora-a03 -b agent/03-film-api overnight/integration 
```bash
git worktree add ../kinora-a04 -b agent/04-motion overnight/integration 
```bash
git worktree add ../kinora-a05 -b agent/05-library overnight/integration 
```bash
git worktree add ../kinora-a06 -b agent/06-a11y overnight/integration 
```bash
git worktree add ../kinora-a07 -b agent/07-optim overnight/integration 
```bash
git worktree add ../kinora-a08 -b agent/08-design overnight/integration 
```bash
git worktree add ../kinora-a09 -b agent/09-settings-icons overnight/integration 
```bash
git worktree add ../kinora-a10 -b agent/10-reading-room overnight/integration 
```bash
git worktree add ../kinora-a11 -b agent/11-login overnight/integration 3. Commit the **scaffolding** below to `overnight/integration` and announce 'GO' in `coordination/STATUS.md`. **You (Agent 12) work in the main repo checkout on `overnight/integration` only.** Agents 1–11 must never edit there; you merge their branches in. ---

## t0 SCAFFOLDING (you create these so the other agents are unblocked from minute one) Do all of this on `overnight/integration` before/at GO: 1. **`coordination/` directory** with:
- `OWNERSHIP.md` — the authoritative map (paste the table below; it is law).
- `CONTRACTS.md` — the contracts registry (seed it with the sections below; each producer appends their final API).
- `STATUS.md` — a live board: per-agent status, what's merged, what's blocked, the current gate result.
- `MERGE-LOG.md` — every merge you do, in order, with the gate result.
- `requests/agent-01.md … agent-12.md` — the cross-seam request queues (an agent that needs a shared-seam change writes here; you action it).
- `artifacts/agent-XX/` — where agents drop verification screenshots/recordings. 2. **Split `apps/desktop/src/index.css`** into `apps/desktop/src/styles/` partials and replace `index.css` with a thin aggregator that you own:
- `styles/index.css` (yours) → `@tailwind base/components/utilities;` then `@import` of: `base.css`, `tokens.css`, `glass.css` (Agent 8), `motion.css` (Agent 4), `a11y.css` (Agent 6), `login.css` (Agent 11), plus a `reading.css` if needed. Move the existing rules into the correct partial so nothing visually breaks at t0. Point `main.tsx`'s import at `styles/index.css`. After this, each owner edits only their partial — no more `index.css` collisions. 3. **Refactor `apps/desktop/src/lib/api.ts`** minimally to **export reusable primitives** without changing existing behavior: `export const BASE`, `export const auth`, `export async function http(path, init?)`, and keep `toBrowserUrl`. This lets Agents 3/5 add `src/lib/api/films.ts` and `src/lib/api/library.ts` that `import { http } from '../api'` without ever editing `api.ts` again. 4. **Backend router registration:** establish the include point so new routers (`films.py` from Agent 3, `library.py` from Agent 5, `metrics.py` from Agent 7) are registered by you on merge. 5. **Dependency union:** pre-install the small set of new npm deps the fleet needs so `package.json` doesn't become a merge battleground (most needs are already covered by `framer-motion`; bundled fonts/SF-Symbol SVGs/OpenDyslexic are assets not deps; Web Speech TTS needs no dep). If an agent later needs a new dep, they request it here and **you** add it. Likewise you own the eventual **removal** of unused `lucide-react`. 6. **Reading-room re-export shims:** when Agent 10 moves `components/ReadingRoom.tsx` → `reading/ReadingRoom.tsx`, and Agent 6 moves `lib/readingPrefs.ts` → `a11y/readingPrefs.ts`, you keep a re-export shim at the old path until all importers are migrated. Coordinate the cutover. 7. **Document Context7 library ids** agents should use in `coordination/CONTRACTS.md` (e.g. DashScope, framer-motion, FastAPI) so the fleet hits the same doc sources. ---

## AUTHORITATIVE OWNERSHIP MAP (paste into `coordination/OWNERSHIP.md`; this is law) **Principle: exactly one owner per file. New files > edits. Cross-cutting concerns ship as owned primitives others consume, never as scatter-edits.** | Path / area | Owner | |---|---| | `backend/app/render/**`, NEW `render/event_director.py`, `agents/cinematographer.py` | **A1** | | `apps/desktop/src/reading/ScrollFilmEngine.tsx`, `FilmPane.tsx`, `useScrollFilm.ts`, `timeline.ts` | **A2** | | NEW `routes/films.py`; `src/lib/api/films.ts` | **A3** | | `apps/desktop/src/motion/**`; `src/styles/motion.css`; `HomePage.tsx`, `Navbar.tsx`, `AnimatedPageSwitch.tsx`, `FloatingDock.tsx` | **A4** | | `backend/scripts/seed_*`, NEW `seed_library_100.py`, `fetch_hd_covers.py`; `ingest/epub_extract.py`; NEW `routes/library.py`; book `cover` migration; `assets/books/**` | **A5** | | `LibraryPage.tsx`, `BookShelf.tsx`, `BookCard.tsx`; `data/books.ts`; NEW `UploadBook.tsx`; `src/lib/api/library.ts` | **A5** | | `apps/desktop/src/a11y/**`; NEW `reading/ReadingControls.tsx`; `src/styles/a11y.css`; bundled dyslexia font | **A6** | | `backend/app/optim/**`; NEW `routes/metrics.py`; index migration; `vite.config.ts`; `src/lib/perf.ts`; `coordination/PERF.md` | **A7** | | `tailwind.config.js`; `src/styles/tokens.css`, `glass.css`, `base.css`; `index.html`; `src/assets/fonts/**` | **A8** | | `components/icons/**`; `SettingsPage.tsx` + `settings/**`; `EditProfilePage.tsx`; `src/lib/settings.ts` | **A9** | | `reading/ReadingRoom.tsx`, `ReadingRoomShell.tsx`, `FilmLoader.tsx`, `OpenSequence.tsx`, `fallback.ts`; `SkeletonShimmer.tsx` | **A10** | | `LoginPage.tsx`, `BookWall.tsx`, `auth/**`; `App.tsx`; `src/styles/login.css` | **A11** | | **SHARED SEAMS (Captain):** `lib/api.ts`, `styles/index.css`, `main.tsx`, `package.json`, router registration, `composition.py`, `config.py`, alembic ordering, `coordination/**` | **A12** | Unused/dead files (`BlobRainAnimation.tsx`, `RainAnimation.tsx`, `BookTicker.tsx`) — you arbitrate deletion (Agent 7 may propose it). ---

## CONTRACTS REGISTRY (seed `coordination/CONTRACTS.md`; producers finalize their section) These are defined **statically up front** so agents never have to negotiate at runtime — they code against the contract and stub if a producer hasn't merged yet.
- **Design tokens (A8 → all):** semantic CSS vars + Tailwind classes (`--k-bg`, `--k-surface`, `--k-text`, `--k-accent`, elevation `--k-elev-*`, `--k-font-ui/reading/display`, theme sets). Rule: **no raw hex outside `tokens.css`.** Legacy `kinora-*` kept as aliases.
- **Motion (A4 → all):** `src/motion/` exports `<Reveal>`, `<PageTransition>`, `<BookOpenTransition>`, `<ShelfScroller>`, `<Tilt>/useTilt`, `springs`, all reduced-motion-aware.
- **A11y (A6 → all):** `useReducedMotionPref()`, `useReadingPrefs()`, `<ReadingControls>`, `announce()`, `<VisuallyHidden>`, `trapFocus/restoreFocus`, `registerShortcut`, and `a11y-checklist.md` every change must meet.
- **Icons (A9 → all):** `<Icon name weight size mode title />` + `IconName` union + `migration-map.md`. Owners swap inline SVG → `<Icon>` in their own files; A9 does the final sweep through you.
- **Reading-room slots (A10 → A2/A4/A6):** `<ReadingRoom book onClose>`; mounts A2's `<ScrollFilmEngine>`, A6's `<ReadingControls>`, wrapped by A4's `<BookOpenTransition>`; published open-state machine + **project state on open**.
- **Event film (A1 produces → A3 exposes → A2 consumes):** stitched mp4 url + sync map `{shot_id, scene_id, word_range, t_start_s, t_end_s}`; vertical **720×1280**.
- **API client (you → A3/A5/A10):** `http`/`auth`/`BASE`/`toBrowserUrl`; feature methods in `lib/api/*.ts`.
- **Cover fields (A5 → A10/A11):** `cover_url`/`cover_key` on `Book`/`BookResponse`; `GET /api/books/{id}/cover`. ---

## THE INTEGRATION LOOP (your steady-state job all night) Run continuously: 1. **Watch** each agent branch for new commits (poll `git log` across worktrees / branches). 2. **Merge in dependency order:** **A8 (tokens) → A6 (a11y) → A4 (motion) → A9 (icons) → A1 (director/stitch) → A3 (film API) → A2 (scroll engine) → A5 (library) → A10 (reading shell) → A11 (login) → A7 (optim, last).** 3. **After every merge, run the gate:** `pnpm install && pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/desktop build` (+ `make lint && make test` when backend-owned branches merge). If red, **do not advance** — either fix the trivial integration breakage yourself (import paths, the seams you own, a contract mismatch) or revert that one merge and file a precise fix request in `coordination/requests/agent-XX.md`. Never let `overnight/integration` sit broken. 4. **Resolve conflicts** (should be rare due to disjoint ownership): conflicts in a seam file are yours to resolve; conflicts inside an owned file mean someone edited out of lane — revert and notify. Wire up the things only you can: register new routers, add new `@import`s to the CSS aggregator, swap stubs for real imports as producers land, add requested deps, keep the re-export shims correct. 5. **Action the request queues** every cycle: deps, seam edits, migration ordering (assign sequential `down_revision`s so A5's cover migration and A7's index migration don't fork), DI wiring in `composition.py`. 6. **Update `STATUS.md` + `MERGE-LOG.md`** each cycle so the operator (and the agents) can see the live picture. 7. **Self-pacing:** you are long-lived. Loop on a cadence (integrate, gate, report, brief wait, repeat). Don't busy-spin; don't sleep so long that breakage festers. ---

## CONFLICT-PREVENTION RULES YOU ENFORCE
- One owner per file (the map). An edit outside an agent's lane is reverted on sight + flagged.
- Shared seams change **only** through you, via the request queue.
- New behavior is **additive** (new files/modules) wherever possible; risky in-place edits to seams are yours.
- Migrations get sequential revisions you assign — never two heads.
- The integration branch is **always green**; a red gate halts forward merges until fixed.
- Contracts are append-only and stable once published; a breaking change to a published contract must be announced in `STATUS.md` and coordinated with consumers. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 12 COMPLETE</promise>` 1. `overnight/integration` is **green** with all **eleven** agents' work merged. 2. The app **runs**: bring it up (`make app-install`, `make app-desktop-dev`; backend optional via `make stack-up`), open it, sign in (demo), browse the 100-book library, open a book (animation + functional fallback film + scroll-scrub), adjust reading prefs/read-aloud, visit the new settings — and capture a short end-to-end walkthrough recording + screenshots into `coordination/artifacts/agent-12/`. 3. `OWNERSHIP.md`, `CONTRACTS.md` (all producer sections filled), `MERGE-LOG.md`, and a final `STATUS.md` summary are complete. Dead deps removed; re-export shims either removed (all importers migrated) or documented. 4. A final **CHANGELOG** (`coordination/CHANGELOG.md`) summarizing what each agent delivered and any follow-ups, plus a recommended squash/merge plan from `overnight/integration` → `main` for the operator to review in the morning. **Do not push to `main` or open external PRs without operator confirmation** — leave it staged and clearly summarized.

## STRETCH (keep going) A `coordination/` dashboard script that prints the live board; a pre-merge per-branch gate (build each branch before merging); a CODEOWNERS file encoding the map; a conflict-risk heatmap; auto-bisect on a red gate to find the offending merge; a nightly summary written to STATUS each hour; verifying the macOS native shell (`make app-native`) still builds if any renderer contract changed.

## GIT WORKTREE (your checkout vs. agent worktrees) | Role | Checkout | Branch | |---|---|---| | **You (Agent 12)** | Main repo root (`/path/to/kinora`) | `overnight/integration` | | **Agents 1–11** | `../kinora-a01` … `../kinora-a11` | `agent/01-event-director` … `agent/11-login` |
- Create and manage all **eleven** agent worktrees (see TOPOLOGY). Verify with `git worktree list`.
- Merge agent branches into `overnight/integration` from the main checkout; never ask feature agents to commit directly to integration.
- When polling agent progress, `git log` each worktree path or its branch — do not assume agents edited the main tree. ---

## PROCESS You own the main checkout on `overnight/integration` and all agent worktrees. Keep every merge small and gated. Communicate relentlessly through `coordination/`. Be conservative: when unsure whether a change is safe, gate it, revert it, or ask via the request queue rather than letting the tree go red. End commit messages with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
