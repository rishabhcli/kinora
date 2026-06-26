# MISSION — AGENT 10: The Book-Open Film Experience (fully functional + an animation) You are the experience engineer who owns Kinora's single most important moment: **opening a book.** The product owner's mandate, verbatim in spirit: *'the app should be fully functional in the video that gets loaded for each book when opening — and there should be an animation.'* Translated: when a reader opens a book, the film for that book must **load reliably and play, fully functional, every time**, wrapped in a **beautiful opening animation** — and it must never show a broken, empty, or frozen experience, even with live video generation turned off. You own the **reading-room shell**: the orchestration, lifecycle, loading/progress/fallback states, and the open/close choreography. You compose Agent 2's scroll-film engine, Agent 6's reading controls, and Agent 4's open transition into one flawless whole. Overnight, no ceiling. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** Electron SSE/fetch, HTMLVideoElement, EventSource, session lifecycle patterns. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## SYSTEM DESIGN (your lane)
- **Project state on book open:** when a book opens, restore **project state** — last read position, session id, buffered-ahead status, current event index — from backend + local persistence. Publish the state shape in `CONTRACTS.md` (Agent 3's events API should include resume fields).
- **User edit regeneration:** expose in-room affordance for 'change this moment' → `POST /sessions/{id}/comment`; hand user intent to backend regen pipeline; show progress via existing SSE (`regen_done`). Coordinate with Agent 6 for accessible controls.
- **Local mashed video:** prefer locally cached stitched event mp4s when available; stream from object store as fallback. ---

## GROUND TRUTH (read first)
- The current reading room is `apps/desktop/src/components/ReadingRoom.tsx` (~587 lines — the hottest file in the app). It already: loads book meta → pages (≤60) → shots → seeds ready clips → `createSession` → opens an SSE stream (`clip_ready`, `buffer_state`) → primes the scheduler with `postIntent(0)`. It has a fallback to bundled `/generated/film-NN.mp4` (hash of book id) + canned text when there's no backend/404, and a 3D cover-flip on open. You will **refactor this into a clean shell** under `src/reading/` and split responsibilities to the right owners (below).
- **`KINORA_LIVE_VIDEO` is OFF by default and stays off.** With it off, the scheduler does **not** promote COMMITTED live renders — so in normal local runs the film is the **Ken-Burns/fallback** path. Your 'fully functional' experience must be *fully functional on the fallback path*: the bundled/Ken-Burns mp4s must load, play, scrub, and sync beautifully. Do not rely on live Wan to look good.
- SSE events available (`lib/api.ts` `openSessionEvents`): `clip_ready`, `buffer_state`, `keyframe_ready`, `scene_stitched`, `agent_activity`, `budget_low`, `regen_done`. Use `agent_activity`/`buffer_state`/`scene_stitched` to drive a real **generation-progress / crew-activity** affordance while the film warms up.
- The film URL rewrite contract: `toBrowserUrl()` maps `minio:9000`→`localhost:9000` and strips presigned queries. Use it.
- Read `CLAUDE.md` and `kinora.md` §4.5–4.9 (scheduler/buffer), §9.6 (stitch/sync). Consume Agent 8 tokens, Agent 9 `<Icon>`, Agent 6 a11y. The actual film rendering + scroll-scrub engine is **Agent 2's** (`<ScrollFilmEngine>`); the open/close motion primitive is **Agent 4's** (`<BookOpenTransition>`). ---

## YOUR LANE — OWNERSHIP (edit ONLY these)
- The reading-room shell: refactor `apps/desktop/src/components/ReadingRoom.tsx` → **`apps/desktop/src/reading/ReadingRoom.tsx`** (you own this file and the migration; leave a re-export so existing imports resolve, coordinated with Agent 12). Supporting NEW files you own under `src/reading/`: `FilmLoader.tsx` (the load/warm-up state machine), `OpenSequence.tsx` (opening-animation choreography), `ReadingRoomShell.tsx`, `fallback.ts` (the no-live-video path).
- `apps/desktop/src/components/SkeletonShimmer.tsx` (loading skeleton).
- You DEFINE and own the **reading-room slot contract** (below) that Agents 2/4/6 plug into. **DO NOT TOUCH (other owners — you MOUNT their components via the slot contract):** `src/reading/ScrollFilmEngine.tsx`/`FilmPane.tsx`/`useScrollFilm.ts` (Agent 2), `src/reading/ReadingControls.tsx` + reading prefs (Agent 6), `src/motion/BookOpenTransition.tsx` (Agent 4), `HomePage.tsx`/nav shell (Agent 4 — it renders `<ReadingRoom book={selectedBook} onClose=…>`), `lib/api.ts` base client (Agent 12), `index.css`/`styles/*`/`tailwind.config.js` (8/4/6), backend. **Shared seams (request file → Agent 12):** `lib/api.ts`, the `ReadingRoom` re-export shim, `main.tsx`, `package.json`. ---

## CONTRACTS
- **You PUBLISH the reading-room slot contract (append to `coordination/CONTRACTS.md`):**
- The shell's own prop: `<ReadingRoom book: Book|null, onClose():void />` (what Agent 4's nav shell renders).
- Slot for Agent 2: mounts `<ScrollFilmEngine book pages shots sessionId prefs onProgress />` and feeds it the loaded data + live session. Define exactly what data you load and hand down.
- Slot for Agent 6: mounts `<ReadingControls prefs onChange />` and reads `useReadingPrefs()`.
- Wrap with Agent 4's `<BookOpenTransition originRect cover onOpened onClosed />`.
- Publish the **open-state machine** states so the loading/progress UI is predictable: `idle → opening(anim) → loading(meta/pages/shots) → warming(session+first frame) → ready → reading → closing`.
- **You CONSUME:** Agent 2 (`<ScrollFilmEngine>`), Agent 4 (`<BookOpenTransition>`), Agent 6 (`<ReadingControls>`, `useReadingPrefs`, focus/announce), Agent 8 (tokens), Agent 9 (`<Icon>`). Stub any not-yet-merged producer against its contract; Agent 12 swaps the real import at integration. ---

## THE BUILD — WORKSTREAMS

### WS1 — The open-state machine (fully functional, every time) Build `FilmLoader.tsx` as an explicit state machine for opening a book: meta → pages → shots → seed ready clips → create session → open SSE → prime scheduler → first frame decoded → ready. Handle EVERY failure path gracefully: 404/no-backend → fallback film; slow ingest (book still `IMPORTING`/`ANALYZING`) → show progress, not an error; partial shots → play what's ready and fill as `clip_ready`/`scene_stitched` arrive; network drop → reconnect SSE; empty/failed clip → never show a black void (hold last frame / fallback). Acceptance: open any book in any state (ready, mid-ingest, no-backend) and you always get a coherent, playing experience — verified across all three.

### WS2 — The opening animation Build `OpenSequence.tsx` using Agent 4's `<BookOpenTransition>`: from the tapped cover's on-shelf rect, the book opens/lifts and dissolves into the first film frame, chrome (text column, controls, progress rail) settling in after. The first film frame must be ready (or a tasteful poster/keyframe) before the reveal completes so there's no flash-of-empty-video. Closing reverses cleanly and preserves the reader's place. Reduced-motion: elegant fade. Acceptance: open→read→close feels like one continuous, premium motion with zero flashes or layout jumps; smooth at 60fps.

### WS3 — Warm-up / generation progress (make waiting delightful) While the film warms (especially with live video off, where clips are Ken-Burns/fallback), present a real, honest progress affordance driven by `agent_activity`/`buffer_state`/`scene_stitched`: a tasteful 'preparing your film' state with crew activity, a buffered-ahead indicator, and a skeleton (own `SkeletonShimmer`). It must resolve seamlessly into playback. No spinners-of-doom; no dead air. Acceptance: the warm-up state always transitions into a playing film and clearly communicates what's happening.

### WS4 — Compose the flawless whole + robustness Wire the shell: mount Agent 2's `<ScrollFilmEngine>`, Agent 6's `<ReadingControls>`, the progress/buffer rail, the top bar (back/title/settings with Agent 9 icons), focus trap + Escape-to-close + place restoration (Agent 6 utils), body-scroll lock, and clean teardown (close SSE, release session) on unmount. Cover the fallback path to full parity (bundled mp4 scrubs + reads). Acceptance: the entire open→read→close loop is leak-free (no dangling SSE/sessions), keyboard- and VoiceOver-operable, and rock-solid under rapid open/close. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 10 COMPLETE</promise>` 1. `pnpm --filter @kinora/desktop typecheck && build` green. 2. With `KINORA_LIVE_VIDEO` OFF, opening a seeded book yields a fully functional, playing, scrubbable film with a beautiful opening animation — and opening a non-existent/backend-less book degrades gracefully. Capture recordings/screenshots of: ready-book open, mid-ingest open, no-backend fallback, and the close animation, into `coordination/artifacts/agent-12/`. 3. No console errors; clean teardown verified (open/close 10× without leaking sessions/listeners). 4. The reading-room slot contract + open-state machine published in `coordination/CONTRACTS.md`. `coordination/STATUS.md` updated.

## STRETCH (keep going) A cinematic 'now playing' intro card per book; poster frames generated from the first keyframe; resume-where-you-left-off with a film seek; picture-in-picture / focus mode; ambient room lighting that tints to the film; an end-of-book outro; graceful handling of very long books; preloading the next likely book's first frame; an in-room director/comment affordance (REST `POST /sessions/{id}/comment` regenerates a shot — the WS comment only classifies); offline reading of already-generated films.

## GIT WORKTREE (mandatory — never work in the shared repo root) You MUST work exclusively in your own isolated git worktree. Do not edit files in the main Kinora checkout, on `overnight/integration` directly, or in any sibling agent worktree. | | | |---|---| | **Worktree path** | `../kinora-a10` (sibling directory next to the repo root) | | **Branch** | `agent/10-reading-room` | | **Base** | `overnight/integration` | **Setup** (if Agent 12 has not already created it): cd /path/to/kinora git fetch origin overnight/integration 2>/dev/null || true 
```bash
git worktree add ../kinora-a10 -b agent/10-reading-room overnight/integration cd ../kinora-a10
``` **Rules:**
- Run all commands, edits, tests, and commits from `../kinora-a10` only.
- Merge `overnight/integration` periodically to pick up contracts/tokens/scaffolding: `git merge overnight/integration`.
- Stage only files you own — never `git add -A` blindly.
- Cross-seam changes go through `coordination/requests/agent-10.md`; Agent 12 merges them. ---

## PROCESS Work from your isolated worktree (see GIT WORKTREE above). Small green commits. Stub against engine/controls/transition contracts until merged. Never edit another agent's files — `coordination/requests/agent-10.md` for cross-seam asks. Update `coordination/STATUS.md`. End commit messages with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
