# MISSION — AGENT 9: Settings, Perfected + SF Symbols Everywhere You are a product-UI engineer embedded in **Kinora** (Electron + React + Tailwind at `apps/desktop`, a macOS-first desktop app). Two jobs, tightly related: 1. **Rebuild the Settings experience.** Today `apps/desktop/src/components/SettingsPage.tsx` is a scattered 3-column grid of toggles with **local state only** — it doesn't persist, and it isn't even wired to the real reading prefs. Replace it with a **centralized, gorgeous, macOS-System-Settings-/Apple-Books-grade settings panel**: a categorized sidebar, real persistence, and every setting that actually matters wired up. 2. **Bring SF Symbols to the whole app.** Today icons are ad-hoc inline SVGs scattered across components (`lucide-react` is installed but unused). Build a **single, unified `<Icon>` system using SF Symbols** so the app looks unmistakably native-Apple, and migrate the app onto it. Overnight, no ceiling — make settings feel like a place you enjoy visiting, and make iconography crisp and consistent in every corner. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** Electron safeStorage, macOS SF Symbols usage in web/Electron contexts. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## GROUND TRUTH (read first)
- `SettingsPage.tsx` is a stub: local `useState` toggles (autoScroll, darkMode, notifications, weeklyDigest, analytics, soundEffects), not persisted, not connected to `lib/readingPrefs.ts`. **Reading-specific** prefs (font/theme/spacing/read-aloud) are owned by **Agent 6** (`<ReadingControls>` + `useReadingPrefs`) — your settings panel must *embed/surface* them, not re-implement them. Coordinate.
- Icons today: hand-rolled inline `<svg>` in `Navbar.tsx` (Home/Library/Watch/Heart/Notes/Search + profile-menu icons), `Greeting.tsx` (sun/moon), `BookShelf.tsx` (arrows), `LoginPage.tsx` (Google/Apple/GitHub), `GooeySearch.tsx`. `lucide-react@0.400` is in `package.json` but unused.
- **SF Symbols strategy (decide + document):** SF Symbols is Apple's system icon set. In Electron you cannot reference the system font's symbols reliably cross-process, so the robust approach is to **vendor SF Symbols as SVGs** (exported from Apple's SF Symbols app / an SF-Symbols-compatible open set) behind a typed `<Icon name=…>` API, with weight/scale/rendering-mode props mirroring SF Symbols semantics. On macOS you may optionally use the `'SF Pro'` system font for text-adjacent glyphs. Document your choice in the contract; ensure licensing is respected (ship only assets you're permitted to).
- This is macOS-first but cross-platform Electron — icons must render identically on Windows/Linux (so: bundled SVGs, not OS-dependent).
- Read `CLAUDE.md`. Consume Agent 8 tokens (icon colors), Agent 6 a11y (icon-button labels), Agent 4 motion (settings transitions). ---

## YOUR LANE — OWNERSHIP (edit ONLY these)
- NEW dir **`apps/desktop/src/components/icons/`**: `Icon.tsx` (the unified component), the SF-Symbols SVG asset set, a typed `IconName` union, and `migration-map.md` (old inline SVG → SF Symbol name).
- **`apps/desktop/src/components/SettingsPage.tsx`** — full rebuild. NEW supporting files under a `src/components/settings/` dir as needed (sidebar, sections, rows, controls).
- `apps/desktop/src/components/EditProfilePage.tsx` — bring into the settings system / polish.
- Settings persistence: NEW `apps/desktop/src/lib/settings.ts` (a small settings store — localStorage now, structured for future backend sync; do NOT duplicate Agent 6's reading prefs — compose them). **DO NOT TOUCH (other owners) — provide `<Icon>` + a migration map; owners adopt it in their own files:** `Navbar.tsx`/`HomePage.tsx` (Agent 4), `LoginPage.tsx` (Agent 11), `LibraryPage.tsx`/`BookCard.tsx`/`BookShelf.tsx` (Agent 5), reading room (Agent 10), `lib/readingPrefs.ts`/`<ReadingControls>` (Agent 6), tokens/CSS (Agent 8), backend. You may do a **final coordinated icon sweep** of leftover files at the end, through Agent 12, once owners have merged. **Shared seams (request file → Agent 12):** `package.json` (remove unused `lucide-react`, or add an icon tool — coordinate with Agent 7), `main.tsx`, and the final cross-file icon-migration sweep. ---

## CONTRACTS
- **You PUBLISH (append to `coordination/CONTRACTS.md`):**
- `<Icon name: IconName, size?, weight?: 'ultralight'|'light'|'regular'|'medium'|'semibold'|'bold', mode?: 'monochrome'|'hierarchical', className?, title? />` — the single icon API; `title` drives the accessible label (or `aria-hidden` when decorative).
- The `IconName` union + the `migration-map.md` so every agent can swap their inline SVGs to `<Icon>` in their own files.
- The settings categories/structure + `lib/settings.ts` API (so the nav/profile entry from Agent 4 opens the right panel).
- **You CONSUME:** Agent 8 tokens (icon stroke/fill via `currentColor`/tokens), Agent 6 a11y (every icon-only button gets a label; honors the checklist), Agent 6's `<ReadingControls>`/`useReadingPrefs` (embed reading settings), Agent 4 motion (panel/section transitions). Stub against contracts if a producer hasn't merged. ---

## THE BUILD — WORKSTREAMS

### WS1 — The `<Icon>` system Build `Icon.tsx` + the SF-Symbols SVG set covering everything the app needs (nav, media controls, library/genre, settings, profile, social, status, reading controls, upload). Mirror SF Symbols weight/scale/rendering semantics. Crisp at all sizes, `currentColor`-driven, tree-shakeable, accessible by default. Publish `IconName` + `migration-map.md`. Acceptance: `<Icon name='book.fill' />` renders pixel-crisp at 16/20/24/32px and matches Apple's visual language.

### WS2 — Settings architecture & persistence Rebuild settings as a centralized panel: a left **category sidebar** (e.g. General, Appearance, Reading, Playback/Film, Notifications, Privacy, Account, About) with a clean detail pane — exactly the macOS System Settings / Apple Books feel. Build `lib/settings.ts` for real persistence (localStorage now; structured for backend sync). Every control reflects and writes real state. Acceptance: changing a setting persists across reloads and actually affects the app; nothing is a dead toggle.

### WS3 — Wire the settings that matter Connect real behavior: Appearance (theme/contrast/reduced-motion/reduced-transparency overrides — via Agent 6's hooks), Reading (embed Agent 6's `<ReadingControls>` so the same prefs surface in Settings and in the reading room), Playback/Film (autoplay, scrub sensitivity, captions — coordinate Agent 1/8), Notifications (Electron `kinora:notify` exists), Privacy/Analytics, Account (sign out, profile via `EditProfilePage`), About (version/credits). Acceptance: each section drives a real subsystem; no placeholder sections.

### WS4 — SF Symbols across the app (migration) In files you own, use `<Icon>` exclusively. Publish the migration map and request each owner adopt `<Icon>` in their files (they edit their own). Then, via Agent 12, do a **final sweep** to replace any remaining inline SVGs/`lucide` references with `<Icon>`, and remove the unused `lucide-react` dep. Acceptance: the app has one icon system; no orphaned inline icon SVGs remain (except intentional brand logos); `lucide-react` is gone. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 09 COMPLETE</promise>` 1. `pnpm --filter @kinora/desktop typecheck && build` green. 2. Screenshots of the new Settings panel (multiple categories) + an icon-gallery screenshot in `coordination/artifacts/agent-11/`. 3. Settings persist across reload and drive real behavior; reading prefs are shared (not duplicated) with Agent 6. 4. `<Icon>` API + `IconName` + `migration-map.md` + settings structure published in `coordination/CONTRACTS.md`. `coordination/STATUS.md` updated.

## STRETCH (keep going) Searchable settings; keyboard-navigable sidebar (with Agent 6); per-setting reset-to-default + 'what changed'; settings sync scaffolding to the backend; SF Symbols **variable-color** + **hierarchical** rendering; animated symbol transitions (with Agent 4, e.g. play↔pause morph); a symbol-picker dev tool; macOS menu-bar parity for key settings; import/export settings; a polished About screen with app branding.

## GIT WORKTREE (mandatory — never work in the shared repo root) You MUST work exclusively in your own isolated git worktree. Do not edit files in the main Kinora checkout, on `overnight/integration` directly, or in any sibling agent worktree. | | | |---|---| | **Worktree path** | `../kinora-a09` (sibling directory next to the repo root) | | **Branch** | `agent/09-settings-icons` | | **Base** | `overnight/integration` | **Setup** (if Agent 12 has not already created it): cd /path/to/kinora git fetch origin overnight/integration 2>/dev/null || true 
```bash
git worktree add ../kinora-a09 -b agent/09-settings-icons overnight/integration cd ../kinora-a09
``` **Rules:**
- Run all commands, edits, tests, and commits from `../kinora-a09` only.
- Merge `overnight/integration` periodically to pick up contracts/tokens/scaffolding: `git merge overnight/integration`.
- Stage only files you own — never `git add -A` blindly.
- Never edit another agent's files except the final coordinated icon sweep via Agent 12. ---

## PROCESS Work from your isolated worktree (see GIT WORKTREE above). Small green commits. Update `coordination/STATUS.md`. End commit messages with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
