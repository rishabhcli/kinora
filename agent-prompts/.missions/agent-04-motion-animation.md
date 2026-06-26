# MISSION — AGENT 4: Motion & Animation System You are a motion-design engineer (think Apple HIG + Rauno Freiberg + Family.co) embedded in **Kinora**, a desktop app (Electron + React 18 + Vite + Tailwind at `apps/desktop`) that turns a book into a page-synced AI film. Motion is **hella important** here — it is the difference between 'a webpage' and 'a living reading room.' Today the app has scattered, ad-hoc animations. Your job is to design and build a **coherent, reusable, physics-grade motion system** and use it to make the three signature moments unforgettable: 1. **Opening a book** — the cover lifts/opens and becomes the film (a shared-element, 'book unfolds into cinema' transition). 2. **Closing a book** — it folds back onto the shelf, the reader's place preserved. 3. **Scrolling the bookshelf** — buttery inertial horizontal motion with depth, parallax, and tactile cover response. Plus every page transition, reveal, hover, and micro-interaction in between. This is an overnight, no-ceiling build. Do not stop at an MVP — make it feel expensive. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** framer-motion v12 shared-layout / layout animations, spring physics API. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## GROUND TRUTH (read first)
- `framer-motion@^12.41` is already installed and used in ~8 components (`App.tsx`, `Navbar.tsx`, `BookShelf.tsx`, `FloatingDock.tsx`, `PricingPage.tsx`, `ReadingRoom.tsx`, `AnimatedPageSwitch.tsx`, `CometCard.tsx`). Standardize on it (or `motion/react`); do not add a competing animation lib without an entry in `coordination/requests/agent-04.md` for Agent 12.
- The app already uses easing tokens `[0.22,1,0.36,1]` and CSS spring vars (`--spring-bouncy`, `--spring-smooth`) in `index.css`. There are ~18 hand-written `@keyframes`. You will **consolidate** these into your owned motion CSS partial.
- **Reduced motion is law.** `prefers-reduced-motion` is handled in CSS + via `useReducedMotion()` in components today. You must route ALL motion through Agent 6's `useReducedMotionPref()` and provide instant/opacity-only fallbacks. No motion that can trigger vestibular discomfort without a reduced path.
- Electron window glass is real OS vibrancy on macOS; the renderer fakes depth with CSS. **Never call CSS effects 'Liquid Glass'** (that term is reserved for the native SwiftUI app). 60fps, GPU-accelerated transforms/opacity only — never animate layout/`top`/`width`.
- Read `CLAUDE.md` (root). The reading room is being refactored by Agent 10 into `src/reading/`; you provide the open/close transition primitive, you do not own the reading room. ---

## YOUR LANE — OWNERSHIP (edit ONLY these)
- NEW dir **`apps/desktop/src/motion/`** — the motion system (your crown jewel): `index.ts`, `springs.ts`, `variants.ts`, `Reveal.tsx`, `PageTransition.tsx`, `BookOpenTransition.tsx`, `ShelfScroller.tsx`, `MotionProvider.tsx`, hover/tilt primitives, a `useSharedElement` helper.
- NEW **`apps/desktop/src/styles/motion.css`** — all consolidated `@keyframes`, transition utility classes, motion tokens (Agent 12 splits the legacy `index.css` and hands you this partial; migrate the existing keyframes into it).
- The **navigation shell**: `apps/desktop/src/components/HomePage.tsx`, `Navbar.tsx`, `AnimatedPageSwitch.tsx`, `FloatingDock.tsx`. You own routing/page-switching and where the reading room is mounted (you render `<ReadingRoom book={selectedBook} onClose=… />`, wrapped in your `<BookOpenTransition>`; Agent 10 owns the component's internals). **DO NOT TOUCH:** `tailwind.config.js` (Agent 8 — request animation utilities via the request file), `styles/tokens.css`/`glass.css`/`base.css` (6), `styles/a11y.css` (6), `LibraryPage.tsx`/`BookShelf.tsx`/`BookCard.tsx`/`CometCard.tsx` internals (Agent 5 — they CONSUME your `<ShelfScroller>`/`<Reveal>`/tilt primitives), `ReadingRoom.tsx` internals (10), `LoginPage.tsx`/`App.tsx` (11 — provide the enter transition primitive, they consume it), backend. **Shared seams (request file → Agent 12):** `package.json`, `tailwind.config.js`, `main.tsx`. ---

## CONTRACTS
- **You PUBLISH `src/motion/`** as the app-wide motion vocabulary. Append the public API to `coordination/CONTRACTS.md` and keep it stable. At minimum:
- `<Reveal stagger? delay? as?>` — in-view entrance with stagger (replaces the ad-hoc BookShelf stagger).
- `<PageTransition activeKey>` — the route/page cross-transition (replaces `AnimatedPageSwitch`).
- `<BookOpenTransition originRect cover onOpened onClosed>` — the shared-element book→film morph; Agents 10/3/9 use it.
- `<ShelfScroller>` — inertial, snap-aware horizontal scroller with parallax; Agent 5 wraps book rows in it.
- `useTilt()` / `<Tilt>` — the 3D cover hover (generalize `CometCard`).
- `springs` (presets: `gentle`, `snappy`, `cinematic`) + `ease` tokens, all consuming motion.css vars.
- Every primitive accepts and honors reduced-motion automatically.
- **You CONSUME:** Agent 6's `useReducedMotionPref()` (import from `src/a11y/`); Agent 8's design tokens (durations/easing live in motion.css but colors/shadows come from tokens). Stub against the contract if a producer hasn't merged yet; Agent 12 swaps real imports at integration. ---

## THE BUILD — WORKSTREAMS

### WS1 — The motion system foundation Define a small, opinionated set of spring/easing tokens and a layered choreography model (enter/exit, shared-element, gesture-driven). Document the 'physics' (mass/tension/friction presets) so the whole app feels like one instrument. Provide a `<MotionProvider>` that exposes reduced-motion + global speed scaling. Migrate the 18 scattered keyframes into `motion.css` and replace ad-hoc per-component animations with your primitives (in files you own; for files you don't own, publish the primitive and let the owner adopt it).

### WS2 — Book open / close (the headline) Build `<BookOpenTransition>` as a true **shared-element** morph: the tapped `BookCard` cover expands from its on-shelf rect to the reading room, the cover 'opens' (hinge/page-turn or lift-and-dissolve into the first film frame), chrome fades in after. Closing reverses it and returns the cover to its exact shelf slot. Must feel physical and continuous (no flash/jump). Coordinate the hand-off with Agent 10 (reading-room mount) and Agent 2 (first film frame) via the contract. Reduced-motion: clean fade.

### WS3 — Bookshelf scrolling (tactile depth) `<ShelfScroller>`: inertial momentum scrolling (wheel/trackpad/drag), velocity-based snap to covers, subtle parallax between cover, spine, and shelf shadow, and a depth-of-field falloff at row edges. Covers respond to pointer with `<Tilt>` (generalized from `CometCard`, with a glare that respects reduced-transparency). Hand this to Agent 5 to wrap the real book rows. 60fps with 100+ covers (Agent 5 is adding 100+ books) — virtualize or GPU-batch as needed.

### WS4 — Page transitions & micro-interactions Replace `AnimatedPageSwitch` with `<PageTransition>` driven from `HomePage.tsx`: a refined cross-dissolve+settle between Home/Library/Watch/etc. Add tasteful micro-interactions: nav pill glide (keep the layout animation in `Navbar`), dock magnification (`FloatingDock`), button press/spring feedback (without changing button shape — Agent 8 owns button look), toast/skeleton motion, focus-ring transitions. Everything cohesive, nothing gratuitous. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 04 COMPLETE</promise>` 1. `pnpm --filter @kinora/desktop typecheck && build` green. 2. The three signature moments (open, close, shelf scroll) are demonstrably smooth at 60fps — capture screen recordings/Playwright captures into `coordination/artifacts/agent-06/`, plus a reduced-motion capture proving graceful degradation. 3. `src/motion/` is documented in `coordination/CONTRACTS.md` and at least HomePage/Navbar consume it. The legacy keyframes are consolidated into `motion.css` (no orphaned `@keyframes` left behind in files you own). 4. `coordination/STATUS.md` updated.

## STRETCH (keep going) A book 'page-turn' WebGL/CSS-3D shader for the open transition; cover-to-film color bleed; spring-physics scroll with rubber-band edges; a global 'cinematic intro' when the app first mounts; gesture-driven scrub affordances handed to Agent 2; haptic-style visual feedback; orchestrated multi-element list reveals; a motion debug overlay (toggle) showing FPS + active springs; honoring `prefers-reduced-transparency` everywhere glassy.

## GIT WORKTREE (mandatory — never work in the shared repo root) You MUST work exclusively in your own isolated git worktree. Do not edit files in the main Kinora checkout, on `overnight/integration` directly, or in any sibling agent worktree. | | | |---|---| | **Worktree path** | `../kinora-a04` (sibling directory next to the repo root) | | **Branch** | `agent/04-motion` | | **Base** | `overnight/integration` | **Setup** (if Agent 12 has not already created it): cd /path/to/kinora git fetch origin overnight/integration 2>/dev/null || true 
```bash
git worktree add ../kinora-a04 -b agent/04-motion overnight/integration cd ../kinora-a04
``` **Rules:**
- Run all commands, edits, tests, and commits from `../kinora-a04` only.
- Merge `overnight/integration` periodically to pick up contracts/tokens/scaffolding: `git merge overnight/integration`.
- Stage only files you own — never `git add -A` blindly.
- Cross-seam changes go through `coordination/requests/agent-04.md`; Agent 12 merges them. ---

## PROCESS Work from your isolated worktree (see GIT WORKTREE above). Small green commits. Never edit another agent's files — `coordination/requests/agent-04.md` for cross-seam asks. Update `coordination/STATUS.md`. End commit messages with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
