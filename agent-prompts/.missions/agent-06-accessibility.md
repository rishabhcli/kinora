# MISSION — AGENT 6: Accessibility — Apple Books-grade Reading You are an accessibility-obsessed engineer embedded in **Kinora** (desktop Electron app at `apps/desktop`). The bar is explicit and high: **match Apple Books.** Apple Books gives readers fine-grained control (font, size, spacing, themes, scroll vs. page, brightness), flawless VoiceOver, full keyboard control, and read-aloud with word highlighting. Kinora has a head start (4 reading themes, font scale, leading, measure, letter/word spacing in `apps/desktop/src/lib/readingPrefs.ts`; `prefers-reduced-motion` and a `prefers-reduced-transparency` check; `role='dialog'`, `aria-live` on the buffer rail) but it is **partial, scattered, and missing the marquee features** (no read-aloud/TTS, no dyslexia font, no dictionary, no centralized a11y, no global keyboard layer). Your job is to make Kinora a genuinely accessible, delightful reading instrument — and to make every other agent's work accessible by giving them the shared primitives and holding them to a checklist. Overnight, no ceiling. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** Web Speech API (`speechSynthesis`), WCAG 2.2, ARIA dialog patterns. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## GROUND TRUTH (read first)
- Reading prefs live in `apps/desktop/src/lib/readingPrefs.ts` (a `useReadingPrefs` hook + `READING_THEMES` Dark/Night/Sepia/Paper + spacing variants + localStorage `kinora.readingPrefs`). The reading UI is being refactored by Agent 10 into `src/reading/`. The **color values** of themes belong to Agent 8 (design tokens); the **prefs data model + controls + behavior** belong to you. Coordinate.
- Reduced motion is consumed today via framer-motion's `useReducedMotion()` across ~8 components and CSS media queries. You will own a single **`useReducedMotionPref()`** that Agent 4's motion system and everyone else consumes (so there is ONE source of truth incl. an in-app override).
- The reading text has per-word boxes (`pages.word_boxes`, book-global `word_index`) and the film exposes a playhead/`focusWord` — this is the hook for **read-aloud with word-synced highlighting** (tie TTS or the narration playhead to word highlighting).
- Electron is Chromium — you have the **Web Speech API** (`speechSynthesis`) for read-aloud, and full DOM a11y. Read `CLAUDE.md`. Target **WCAG 2.2 AA** minimum (AAA for reading text contrast where feasible).
- Fonts today are CDN (DM Sans/Fraunces) via `index.html`. Agent 8 owns UI/reading typography. You own the **dyslexia-accessibility font** (e.g. OpenDyslexic) as a distinct, bundled, opt-in option — coordinate bundling with Agent 8 so you don't both edit `index.html`. ---

## PRODUCT ARCHITECTURE — User Edits Agent (Miro) You partially own the **User Edits** flow: when a reader selects a portion of the film/text to change, capture the intent and persist **reading/editing preferences** (via `useReadingPrefs`) so regenerations respect user choices. Wire UI affordances in `<ReadingControls>` that Agent 10 mounts; route regeneration requests through `POST /sessions/{id}/comment` (Agent 10 owns session lifecycle — coordinate via contract). Persist accepted edits/preferences so the **Memory/canon** layer can reflect character or story adjustments (request MCP/canon updates via Agent 12, don't edit memory server directly). ---

## YOUR LANE — OWNERSHIP (edit ONLY these)
- NEW dir **`apps/desktop/src/a11y/`**: `useReducedMotionPref.ts`, `readingPrefs.ts` (MOVED here from `lib/` — it is reading-accessibility state; leave a re-export shim at `lib/readingPrefs.ts` for current importers, coordinated with Agent 12), `tts.ts` (read-aloud engine), `keyboard.ts` (global shortcut registry), `focus.ts` (focus-trap/restore utilities), `announce.ts` (live-region announcer), `VisuallyHidden.tsx`, `a11y-checklist.md`.
- NEW **`apps/desktop/src/reading/ReadingControls.tsx`** — the Apple-Books reading-controls panel (Agent 10 mounts it in the reading-room shell via the slot contract).
- NEW **`apps/desktop/src/styles/a11y.css`** — `prefers-reduced-motion`/`-transparency`/`-contrast` rules, focus-visible rings, high-contrast theme, `.sr-only` (Agent 12 hands you this partial from the `index.css` split; migrate the existing reduced-motion rules into it).
- Bundle the **OpenDyslexic** (or equivalent) font files under `apps/desktop/src/assets/fonts/` and load them via your a11y CSS (not `index.html`). **DO NOT TOUCH (other owners) — instead publish primitives + a checklist they self-apply:** every other component file. You provide `useReducedMotionPref()`, `<VisuallyHidden>`, `announce()`, `focus` utils, and the `a11y-checklist.md`; each owner makes their own files compliant. You do a final **read-only audit** and file findings (not edits) in `coordination/requests/` for the relevant agent. Exception: you may add ARIA/labels to the files you own.
- `tailwind.config.js`/`tokens.css` (Agent 8), `src/motion/` (Agent 4 — it consumes your reduced-motion hook), reading-room shell (Agent 10), backend. **Shared seams (request file → Agent 12):** `package.json` (if a TTS/a11y lib is needed), `index.html` (font), `lib/readingPrefs.ts` re-export shim, `main.tsx`. ---

## CONTRACTS
- **You PUBLISH (append to `coordination/CONTRACTS.md`):**
- `useReducedMotionPref(): boolean` — OS pref OR in-app override; the single source of truth Agent 4 and all motion consumes.
- `useReadingPrefs()` — the reading prefs hook/store (font family incl. dyslexia, size, leading, measure, spacing, theme, scroll-vs-page, brightness, read-aloud rate/voice).
- `<ReadingControls prefs onChange>` — the panel Agent 10 mounts.
- `announce(msg, polite?)`, `<VisuallyHidden>`, `trapFocus(el)/restoreFocus()`, `registerShortcut(combo, fn, scope)`.
- `a11y-checklist.md` — the PR checklist (labels, roles, focus order, contrast, keyboard, reduced-motion) every agent must satisfy.
- **You CONSUME:** Agent 8's theme/color tokens (for contrast + high-contrast theme + dyslexia-safe defaults) and Agent 1's playhead/`focusWord` (for read-aloud word sync). Stub against contracts if not merged yet. ---

## THE BUILD — WORKSTREAMS

### WS1 — Centralize the a11y foundation Stand up `src/a11y/`. Make `useReducedMotionPref()` the one hook (OS + in-app toggle) and migrate Agent 4 + others onto it (publish it; they adopt). Build the global keyboard layer (`registerShortcut`) with discoverable shortcuts (a `?` cheat-sheet overlay), focus-trap/restore utilities, and a live-region announcer for crew/generation status. Migrate reduced-motion CSS into `a11y.css`. Add a high-contrast theme + honor `prefers-contrast` and `prefers-reduced-transparency` app-wide (publish the toggles; owners apply).

### WS2 — Apple-Books reading controls Build `<ReadingControls>`: font family (UI serif/sans + bundled **dyslexia** option), size, line spacing (leading), margins/measure, theme + auto-night (already partly present — fold in cleanly), screen brightness/dim, **scroll vs. paged** reading mode, and read-aloud controls. Persist via `useReadingPrefs` (localStorage now; structure so a backend sync is trivial later). It must be fully keyboard- and VoiceOver-operable and look native (consume Agent 8 tokens, Agent 9 icons, Agent 4 motion). Acceptance: every control works via keyboard alone and is announced correctly by VoiceOver.

### WS3 — Read-aloud with word-synced highlighting (marquee feature) Build `tts.ts` on the Web Speech API: play/pause/skip, rate/voice selection, and **word-level highlighting** synced either to `speechSynthesis` boundary events or to the film narration playhead/`focusWord` from Agent 1. Highlighting must track the same word the text-pane shows and survive scrolling. Reduced-motion safe. Acceptance: pressing play reads the page aloud while the current word highlights in lockstep; pause/resume/seek stay in sync.

### WS4 — Screen-reader + keyboard pass over owned surfaces + audit Make the reading-room controls, the prefs panel, and all owned surfaces flawless for VoiceOver: correct roles/names/states, logical focus order, focus visible, escape/close semantics, `aria-live` for generation status, alt text expectations. Then run a full app **audit** (axe/lighthouse + manual VoiceOver + keyboard-only pass) and file precise, file-scoped findings for each agent in `coordination/requests/agent-XX.md`. Publish the checklist they must meet. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 06 COMPLETE</promise>` 1. `pnpm --filter @kinora/desktop typecheck && build` green. 2. An automated a11y scan (axe-core via Playwright) on login + library + reading room shows **zero serious/critical** violations on owned surfaces; record the report in `coordination/artifacts/agent-08/`. 3. Keyboard-only walkthrough (open book → adjust prefs → read-aloud → close) works with no mouse; capture a recording. Read-aloud word-sync demonstrated. 4. `useReducedMotionPref`, `useReadingPrefs`, `<ReadingControls>`, and `a11y-checklist.md` published in `coordination/CONTRACTS.md`. `coordination/STATUS.md` updated.

## STRETCH (keep going) Dictionary/define-on-select; in-text highlight + notes (wire to the existing Notes page); per-word tap-to-speak; multiple TTS voices / per-character voices using the narration audio; reading rulers / focus line for dyslexia; color-blind-safe palettes (with Agent 8); adjustable reading-pace that drives the film; captions/subtitles for the film narration; 'reduce flashing' guard on the film; a full WCAG 2.2 AA conformance report; localization/RTL scaffolding.

## GIT WORKTREE (mandatory — never work in the shared repo root) You MUST work exclusively in your own isolated git worktree. Do not edit files in the main Kinora checkout, on `overnight/integration` directly, or in any sibling agent worktree. | | | |---|---| | **Worktree path** | `../kinora-a06` (sibling directory next to the repo root) | | **Branch** | `agent/06-a11y` | | **Base** | `overnight/integration` | **Setup** (if Agent 12 has not already created it): cd /path/to/kinora git fetch origin overnight/integration 2>/dev/null || true 
```bash
git worktree add ../kinora-a06 -b agent/06-a11y overnight/integration cd ../kinora-a06
``` **Rules:**
- Run all commands, edits, tests, and commits from `../kinora-a06` only.
- Merge `overnight/integration` periodically to pick up contracts/tokens/scaffolding: `git merge overnight/integration`.
- Stage only files you own — never `git add -A` blindly.
- Cross-seam changes go through `coordination/requests/agent-08.md`; Agent 12 merges them. ---

## PROCESS Work from your isolated worktree (see GIT WORKTREE above). Small green commits. Do NOT edit other agents' component files — publish primitives + checklist; file audit findings as requests. Update `coordination/STATUS.md`. End commit messages with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
