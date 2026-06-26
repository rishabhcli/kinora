# MISSION — AGENT 2: Scroll Film Engine (Client Timeline Scrubbing) You own the single most important **client illusion** in Kinora: **scrolling through a book feels like scrubbing one continuous film.** Today `ReadingRoom.tsx` swaps one `<video>` per shot with a 0.55s crossfade — choppy. You replace that with **continuous-timeline scrubbing** of stitched event films from Agent 1, bound to scroll via Agent 5's sync map. Overnight, no ceiling. 60fps, GPU-only transforms, reduced-motion aware. ---

## TOOLING — Superpowers + Context7 (mandatory) Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.
- **Priority lookups:** framer-motion v12 (`motion/react`), React 18 concurrent patterns, HTMLMediaElement / video sync. **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development Use throughout every Ralph loop iteration: 
| Skill / practice | When |
|---|---| | **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. 
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. 
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. 
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. 
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). | ---

## GROUND TRUTH
- Read `CLAUDE.md`, `kinora.md` §4.5–4.9, §9.6. Study `ReadingRoom.tsx` ~189–242 for current `focusWord`/velocity/`postIntent` logic — reproduce inside your engine.
- **`KINORA_LIVE_VIDEO` OFF** — bundled `/generated/film-NN.mp4` and Ken-Burns paths must scrub identically. Never show frozen/empty pane.
- Films are **vertical 720×1280**. Use `toBrowserUrl()` for MinIO→localhost rewrite. ---

## SYSTEM DESIGN (your lane)
- **Local mashed video:** prefer playing stitched event mp4s cached on device when available; fall back to streaming from object store.
- **Preload:** decode next event film on idle when scroll approaches scene boundary (coordinate with Agent 9 perf helpers via contract). ---

## YOUR LANE — OWNERSHIP **Client (own outright):**
- NEW `apps/desktop/src/reading/`: `ScrollFilmEngine.tsx`, `FilmPane.tsx`, `useScrollFilm.ts`, `timeline.ts`. **DO NOT TOUCH:** `ReadingRoom.tsx` shell (Agent 12 mounts you), `films.py`/`films.ts` (Agent 5), `render/` (Agent 1), `index.css`/`styles/*` (Agents 10/4/6), `lib/api.ts` base (Agent 12). **Shared seams → `coordination/requests/agent-04.md`** ---

## CONTRACTS
- **You PUBLISH to Agent 12:** `export function ScrollFilmEngine(props: { book; pages; shots; sessionId?; prefs; onProgress?(fraction:number, focusWord:number):void }): JSX.Element` — film pane + scroll/sync internal. Append props to `coordination/CONTRACTS.md`.
- **You CONSUME:** Agent 5's event film API + sync map; Agent 10 design tokens; Agent 6 `useReducedMotionPref()`; Agent 6 motion primitives from `src/motion/`. ---

## THE BUILD

### WS1 — Scroll → timeline scrubbing Bind scroll fraction → `currentTime` via sync map. Dragging scroll IS scrubbing; release resumes playback. Preserve `postIntent`/`seek` scheduler signalling.
- Velocity-aware: fast scroll = scrub; slow/at-rest = play forward.
- Inertia, scroll-snap to scene boundaries, parallax between text column and film, scrub indicator. GPU transforms only, 60fps.

### WS2 — Cross-event handoff Scrolling between events: crossfade two stitched timelines (≤2 `<video>` layers, event level not shot level).

### WS3 — Fallback parity `live=false` / no backend: bundled mp4 path scrubs and feels identical. ---

## DEFINITION OF DONE When all items pass, output exactly: `<promise>AGENT 02 COMPLETE</promise>` 1. `pnpm --filter @kinora/desktop typecheck && build` green. 2. Scroll scrub frame-accurate to sync map (±1 shot); 60fps under fast flicks; reduced-motion degrades to instant cuts. 3. `ScrollFilmEngine` contract in `coordination/CONTRACTS.md`. Artifacts in `coordination/artifacts/agent-04/`.

## STRETCH Motion-blur on fast scrub; audio-reactive scrub; timeline minimap; prefetch next event decode.

## GIT WORKTREE | **Worktree** | `../kinora-a02` | | **Branch** | `agent/02-scroll-film` | 
```bash
git worktree add ../kinora-a02 -b agent/02-scroll-film overnight/integration cd ../kinora-a02
``` Cross-seam: `coordination/requests/agent-04.md`. End commits with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
