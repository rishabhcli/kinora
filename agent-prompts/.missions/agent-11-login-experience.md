# MISSION — AGENT 11: The Login Experience, Even More Polished You are a UI craftsperson embedded in **Kinora** (Electron + React + Tailwind at `apps/desktop`). The login screen is the first thing anyone sees — it must feel like opening the cover of an extraordinary book. It's already decent (a frosted card over an animated `BookWall` backdrop, social buttons, sign-in/sign-up toggle, demo entry — see `apps/desktop/src/components/LoginPage.tsx` + `BookWall.tsx`, styled in `index.css` lines ~847–1057). Your mandate: **make it even more polished** — a jaw-dropping, cinematic, flawless first impression that sets the tone for the whole product. Overnight, no ceiling. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** framer-motion enter/exit, Electron auth storage, form a11y patterns. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## GROUND TRUTH (read first)
- `LoginPage.tsx` renders: an animated shelves backdrop (`<BookWall rows={4} />`), a left tagline ('Where stories come to life'), a centered frosted card with email/password, a Sign In ↔ Sign Up toggle, Google/Apple/GitHub buttons (inline SVG), a demo-library button, remember-me + forgot-password. Auth goes through `api.loginOrRegister(email, password)` (silent demo fallback) and stores a Bearer token. `App.tsx` toggles login ↔ home with a 0.4s framer-motion fade (no transform, to preserve the fixed navbar).
- **The frosted look must keep text crisp and AA-contrast** (coordinate Agents 6/8).
- **Never call CSS effects 'Liquid Glass'** (native-only term). Electron vibrancy shows through `html.kinora-native` — your backdrop must look right with and without it.
- Read `CLAUDE.md`. Consume Agent 8 tokens (colors/depth/type), Agent 4 motion (`<BookOpenTransition>`/`<Reveal>` + the enter-to-home transition), Agent 9 `<Icon>` (social/field icons), Agent 6 a11y (labels, focus order, keyboard, reduced-motion). The demo login is `demo@kinora.local` / `demo-password-123`. ---

## SYSTEM DESIGN (your lane)
- **Cover cache on login:** on successful auth, prefetch HD **book cover thumbnails** from Agent 5's cover API so `BookWall` and the post-login library feel instant. Warm the cache for demo user's seeded library.
- **On-device first:** login flow must work offline against cached demo creds + cached covers when backend is unreachable. ---

## YOUR LANE — OWNERSHIP (edit ONLY these)
- `apps/desktop/src/components/LoginPage.tsx`, `apps/desktop/src/components/BookWall.tsx`.
- `apps/desktop/src/App.tsx` — the auth gate + the login→home enter transition (you own this seam; use Agent 4's transition primitive).
- NEW `apps/desktop/src/styles/login.css` — all login/auth + `BookWall` styles (Agent 12 hands you this partial from the `index.css` split; migrate the login styles into it).
- NEW supporting files under `src/components/auth/` as needed (form field component, social-button row, ambient backdrop) — yours. **DO NOT TOUCH:** `HomePage.tsx`/home/library/reading (others), `lib/api.ts` base client (Agent 12 — auth methods already exist; request changes via the file), tokens/motion/a11y/icon source (6/2/4/7 — consume them), backend. **Shared seams (request file → Agent 12):** `lib/api.ts` (auth surface), `main.tsx`, `package.json`, the `index.css` aggregator (it imports your `login.css`). ---

## CONTRACTS
- **You CONSUME:** Agent 8 tokens (the whole screen must be token-driven — no hardcoded hex), Agent 4 motion (backdrop life + the enter transition + field micro-interactions), Agent 9 `<Icon>` (replace the inline social/field SVGs), Agent 6 a11y (the form must be perfectly keyboard- and VoiceOver-operable, errors announced, reduced-motion honored). Stub against contracts if a producer hasn't merged; Agent 12 swaps real imports at integration.
- **You PUBLISH:** nothing structural; note in `coordination/STATUS.md` if you change the `App.tsx` enter-transition contract so Agent 4/8 stay aligned. ---

## THE BUILD — WORKSTREAMS

### WS1 — A cinematic ambient backdrop Elevate the backdrop beyond static shelves: a slow, living, depth-rich scene — parallax shelves, drifting light, dust motes, subtle bloom — or an ambient, muted, looping **film teaser** (a bundled vertical clip, tastefully blurred/overlaid; coordinate with Agent 1 if you want a real generated loop). It must be GPU-cheap (60fps), reduced-motion-aware (calm static fallback), and never fight the form's legibility. Acceptance: the backdrop feels alive and premium yet the card text stays perfectly readable.

### WS2 — The form, perfected Refine the frosted auth card: impeccable spacing/rhythm, token-driven depth, crisp typography, real-time validation with gracefully announced errors, password show/hide, loading/success/failure states on submit, a polished Sign In ↔ Sign Up morph, remember-me + forgot-password that behave, and a clearly inviting demo-entry path. Social buttons use `<Icon>` and look native. Keep buttons' established shape (Agent 8 owns button look) — you own layout/states/copy. Acceptance: every field and state is keyboard/VoiceOver-operable, validation is friendly, submit feedback is immediate, legibility is AA.

### WS3 — The enter transition (login → app) Replace the flat 0.4s fade with a deliberate, cinematic hand-off into the home view using Agent 4's primitives — e.g. the card recedes and the library 'opens' behind it — without breaking the fixed navbar anchor (the reason the current transition avoids transforms; solve it properly). Reduced-motion: clean cross-fade. Acceptance: signing in feels like stepping through a threshold into the app, at 60fps, with no layout jump.

### WS4 — Branding & the first 5 seconds Tighten the brand moment: logo lockup, tagline, an opening micro-animation when the app first launches (with Agent 4), and consistent token-driven color/type. Make the very first 5 seconds unforgettable and on-brand. Acceptance: a cold launch into login reads as a flagship product intro. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 11 COMPLETE</promise>` 1. `pnpm --filter @kinora/desktop typecheck && build` green. 2. Recordings/screenshots of: cold launch → login, sign-in/sign-up morph, validation states, and the enter-to-home transition (plus a reduced-motion capture) in `coordination/artifacts/agent-11/`. 3. The form is fully keyboard- and VoiceOver-operable; text is AA-legible over the backdrop; demo login works end to end. 4. Login styles migrated into `styles/login.css`; screen is fully token-driven. `coordination/STATUS.md` updated.

## STRETCH (keep going) A subtle per-launch varied backdrop (different book/scene each time); animated logo; 'continue as guest'/demo with a delightful reveal of the seeded library; OAuth real wiring if backend supports it; passwordless/magic-link scaffolding; an onboarding first-run sequence; localized copy; a dark/light backdrop responsive to time of day (with Agent 8); micro-sound cues (respecting a sound setting from Agent 9).

## GIT WORKTREE (mandatory — never work in the shared repo root) You MUST work exclusively in your own isolated git worktree. Do not edit files in the main Kinora checkout, on `overnight/integration` directly, or in any sibling agent worktree. | | | |---|---| | **Worktree path** | `../kinora-a11` (sibling directory next to the repo root) | | **Branch** | `agent/11-login` | | **Base** | `overnight/integration` | **Setup** (if Agent 12 has not already created it): cd /path/to/kinora git fetch origin overnight/integration 2>/dev/null || true 
```bash
git worktree add ../kinora-a11 -b agent/11-login overnight/integration cd ../kinora-a11
``` **Rules:**
- Run all commands, edits, tests, and commits from `../kinora-a11` only.
- Merge `overnight/integration` periodically to pick up contracts/tokens/scaffolding: `git merge overnight/integration`.
- Stage only files you own — never `git add -A` blindly.
- Cross-seam changes go through `coordination/requests/agent-11.md`; Agent 12 merges them. ---

## PROCESS Work from your isolated worktree (see GIT WORKTREE above). Small green commits. Stub against contracts until merged. Never edit another agent's files — `coordination/requests/agent-11.md` for cross-seam asks. Update `coordination/STATUS.md`. End commit messages with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
