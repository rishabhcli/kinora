# MISSION — AGENT 8: Color, Depth & Typography — Reimagined (NOT the buttons) You are a senior visual/brand designer-engineer embedded in **Kinora** (Electron + React + Tailwind at `apps/desktop`). The app today is a warm-dark, gold-accented 'liquid glass'-imitation aesthetic (`tailwind.config.js` `kinora-*` tokens; a 1231-line `index.css` full of glass surfaces, gradients, and depth tricks; CDN fonts DM Sans + Fraunces). Your mandate: **reimagine the color scheme, the depth/material system, and the typography** into something distinctive, cohesive, and premium — and turn it into a **token system the whole app consumes**, so the redesign propagates everywhere without you touching every component. **HARD CONSTRAINT FROM THE PRODUCT OWNER: do NOT change the buttons.** Keep button shape, size, structure, and behavior exactly as they are. You may only let buttons inherit new *color values* through tokens; you may not restyle their form. Everything else — backgrounds, surfaces, cards, text, depth, gradients, fonts — is yours to reimagine. Overnight, no ceiling: make it look like a flagship Apple-tier product. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** Tailwind CSS v3 theme extension, CSS custom properties, `@font-face` best practices. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## GROUND TRUTH (read first)
- Tokens today: `tailwind.config.js` `theme.extend.colors.kinora` (`bg #181614`, `bg-deep #0e0d0c`, `text #e8e2d8`, `muted`, `subtle`, `gold #d4a44e`, `gold-light`) + `fontFamily` (sans: DM Sans; serif: Fraunces). `index.css` holds `:root` spring vars, glass classes (`.liquid-glass-dock`, `.glass-card`, `.glass-input`, `.glass-control`), 3D-book depth, gradients, and the `#lg-refract` SVG filter.
- **Never call CSS effects 'Liquid Glass.'** Real Liquid Glass is the native SwiftUI app only; Electron fakes depth with `backdrop-filter`/shadows/gradients. Name yours 'depth'/'material'/'frosted.' Electron window vibrancy (macOS) shows through `html.kinora-native` surfaces — your material system must look right both with and without native vibrancy.
- Fonts are loaded from Google Fonts CDN in `index.html`. You may bundle fonts for reliability/perf (coordinate with Agent 7 on loading; coordinate with Agent 6 — Agent 6 owns the separate **dyslexia** font, you own the UI/reading/display typography).
- Reading themes (Dark/Night/Sepia/Paper) live as data in `apps/desktop/src/lib/readingPrefs.ts` (Agent 6 owns the data model/behavior). **You own the color VALUES** of those themes — supply them as tokens Agent 6 consumes. Coordinate.
- Read `CLAUDE.md`. WCAG AA contrast is a floor for all text/background pairs (coordinate the high-contrast theme with Agent 6). ---

## YOUR LANE — OWNERSHIP (edit ONLY these)
- **`apps/desktop/tailwind.config.js`** — the color palette, semantic color scale, fontFamily, boxShadow/depth scale, backdropBlur, radius. (Agent 4 requests animation utilities via the request file; you own the rest.)
- NEW **`apps/desktop/src/styles/tokens.css`** — CSS custom properties: semantic color tokens, depth/elevation tokens, typography tokens, theme token sets (Agent 12 hands you this partial from the `index.css` split).
- NEW **`apps/desktop/src/styles/glass.css`** — the depth/material surfaces (frosted panels, cards, dock, refraction) — reimagined.
- NEW **`apps/desktop/src/styles/base.css`** — reset, body, scrollbar, base typography (Agent 12 hands you these from the split).
- `apps/desktop/index.html` — font loading + the `#lg-refract` SVG filter defs (coordinate with Agent 6 re: the dyslexia font so you don't both edit this; you own UI/reading/display fonts here).
- NEW `apps/desktop/src/assets/fonts/` — bundled UI/reading/display font files (if you bundle). **DO NOT TOUCH:** any component `.tsx` (you change appearance via tokens/classes they already use, NOT by editing components), `styles/motion.css` (Agent 4), `styles/a11y.css` (Agent 6), `lib/readingPrefs.ts` (Agent 6 — supply theme values via tokens), backend. **Do NOT restyle buttons' form** anywhere. **Shared seams (request file → Agent 12):** `package.json` (if a font/tooling dep is needed), `main.tsx`, the `index.css` aggregator (Agent 12 owns; it `@import`s your partials). ---

## CONTRACTS
- **You PUBLISH the design-token contract (append to `coordination/CONTRACTS.md`) — this is the most-consumed contract in the fleet.** Define stable semantic names every agent uses instead of hardcoded hex, e.g.:
- Color: `--k-bg`, `--k-bg-deep`, `--k-surface`, `--k-surface-raised`, `--k-surface-glass`, `--k-text`, `--k-text-muted`, `--k-text-subtle`, `--k-accent`, `--k-accent-strong`, `--k-border`, `--k-border-strong`, plus state colors. Mirror them as Tailwind classes (`bg-surface`, `text-muted`, `border-subtle`, …). Keep the legacy `kinora-*` names working as aliases so nothing breaks.
- Depth: `--k-elev-1..5`, `--k-blur`, material classes `.surface`, `.surface-raised`, `.surface-frosted`.
- Type: `--k-font-ui`, `--k-font-reading`, `--k-font-display`, a modular type scale, and weights.
- Theme sets: tokens for Dark/Night/Sepia/Paper (+ high-contrast) that Agent 6 binds.
- **The rule you enforce:** no agent uses raw hex outside `tokens.css`; everyone consumes tokens. State this in the contract.
- **You CONSUME:** nothing. You may coordinate contrast with Agent 6. ---

## THE BUILD — WORKSTREAMS

### WS1 — Reimagine the palette Design a distinctive, cohesive color system (a refined neutral spine + an evolved accent + supporting hues + semantic states), in both the dark 'reading cinema' mode and any light reading themes. Justify it briefly (mood, references) at the top of `tokens.css`. Express everything as semantic tokens with AA-safe text/background pairings. Map the legacy `kinora-*` palette onto the new system so the whole app shifts at once. Acceptance: the app visibly transforms via tokens alone; no component edits; all text passes AA.

### WS2 — Reimagine depth & material Rebuild the depth/material system in `glass.css`: a consistent elevation scale (shadows + blur + border + highlight), reimagined frosted surfaces/cards/dock, refined gradients, and a tasteful refraction that works WITH macOS vibrancy and degrades gracefully when `prefers-reduced-transparency` is set (coordinate with Agent 6). Make depth feel intentional and layered, not noisy. Acceptance: surfaces read as a coherent material language across login/home/library/reading; no 'muddy glass.'

### WS3 — Reimagine typography Choose and integrate a premium type system (a display face, a UI face, and a reading face — you may keep Fraunces/DM Sans if you elevate their usage, or introduce better). Bundle fonts for perf/reliability (coordinate Agent 7). Define a real modular scale, optical sizing, leading, measure, and tracking tokens. Reading text must be gorgeous and legible; UI text crisp; display type cinematic. Acceptance: typography is consistent and beautiful everywhere via `--k-font-*` tokens; reading-pane defaults feel book-quality.

### WS4 — Theme values + propagation Supply the color VALUES for Dark/Night/Sepia/Paper (+ high-contrast) as token sets for Agent 6 to bind. Audit the app (visually, via the running build + Playwright screenshots) to confirm the redesign reaches every surface through tokens, and file `coordination/requests/agent-XX.md` notes where an owner still hardcodes a color so they migrate to tokens. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 08 COMPLETE</promise>` 1. `pnpm --filter @kinora/desktop typecheck && build` green; the app boots and renders with the new tokens. 2. Before/after screenshots of login, library, and reading room (dark + a light theme) in `coordination/artifacts/agent-10/`, demonstrating the redesign — with **buttons unchanged in form**. 3. The token contract is published in `coordination/CONTRACTS.md`; legacy `kinora-*` aliases still resolve (no broken styles). 4. An AA-contrast check on key text/background pairs (note results). `coordination/STATUS.md` updated.

## STRETCH (keep going) Per-book accent theming driven by cover art (consume Agent 5's cover palette); a light overall UI theme (not just reading); subtle film-grain/paper-grain material; dynamic depth that responds to scroll/parallax (with Agent 4); a tokens design-doc page; dark/light auto by time + ambient; richer state/semantic colors; a 'focus' reading material that dims chrome; verifying the look under macOS vibrancy on/off.

## GIT WORKTREE (mandatory — never work in the shared repo root) You MUST work exclusively in your own isolated git worktree. Do not edit files in the main Kinora checkout, on `overnight/integration` directly, or in any sibling agent worktree. | | | |---|---| | **Worktree path** | `../kinora-a08` (sibling directory next to the repo root) | | **Branch** | `agent/08-design` | | **Base** | `overnight/integration` | **Setup** (if Agent 12 has not already created it): cd /path/to/kinora git fetch origin overnight/integration 2>/dev/null || true 
```bash
git worktree add ../kinora-a08 -b agent/08-design overnight/integration cd ../kinora-a08
``` **Rules:**
- Run all commands, edits, tests, and commits from `../kinora-a08` only.
- Merge `overnight/integration` periodically to pick up contracts/tokens/scaffolding: `git merge overnight/integration`.
- Stage only files you own — never `git add -A` blindly.
- Coordinate the `index.css` split with Agent 12 (it owns the aggregator that imports your partials). ---

## PROCESS Work from your isolated worktree (see GIT WORKTREE above). Small green commits. Never edit component `.tsx` files or buttons' form. Update `coordination/STATUS.md`. End commit messages with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
