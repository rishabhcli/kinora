# CHANGELOG — overnight/integration (Kinora fleet build)

Branch `overnight/integration` off `main@4863a0c`. Eleven feature agents (isolated
worktrees) + Agent 12 (Integration Captain, this branch). Gate held green throughout:
frontend `pnpm --filter @kinora/desktop typecheck && build`, backend `make test`
(408 passed / 0 failed, single alembic head). `KINORA_LIVE_VIDEO` stayed OFF.

## Captain rails (t0, Agent 12)
- `.gitignore` for `.claude/` (per-worktree Ralph state never committed).
- `coordination/` — OWNERSHIP (law), CONTRACTS (registry + Context7 ids), STATUS,
  MERGE-LOG, CAPTAIN-PLAYBOOK, request queues, artifact dirs.
- **`index.css` (1230 lines) → `src/styles/` partials** (byte-exact split, verified) +
  a `postcss-import` aggregator (`styles/index.css`). Owners edit their partial only.
- **`api.ts` primitives** `BASE` / `http` (CONTRACTS §7) so feature API modules compose.
- Backend router registration seam (`api/routes/__init__.py`), dep/lockfile union,
  re-export shims (`lib/readingPrefs`→`a11y`, `components/ReadingRoom`→`reading/`).
- **Producer swap**: reading room wired to the real A2 `ScrollFilmEngine` + A6
  `ReadingControls` (adapters in `reading/producers.tsx`).

## Per-agent deliverables
| Agent | Delivered | Notes for the morning |
|---|---|---|
| **A1** event-director | `render/event_director.py`, `continuity_qa.py`, `shot_grammar.py`; vertical 720×1280 enforced; degrade ladder | backend; 40 render tests green |
| **A2** scroll-film | `reading/ScrollFilmEngine.tsx`, `timeline.ts` (continuous scrub) | consumes A1 sync map; wired via producers.tsx |
| **A3** film-api | `routes/films.py` + `films/contract.py`; `lib/api/films.ts` | ships own `lib/api/http.ts` — follow-up: use shared `http` from `../api` (§7) |
| **A4** motion | `src/motion/**` (Reveal, PageTransition, BookOpenTransition, ShelfScroller, Tilt, springs); `motion.css` | shell page transitions wired |
| **A5** library | `routes/library.py`, `library/{catalog,covers}.py`, cover fields + migration `e843aa7682b2`, public-domain seeds | DB cover_url/cover_key (§8) |
| **A6** a11y | `src/a11y/**` (A11yProvider, focus trap, announce, useReducedMotionPref, readingPrefs, ReadingControls); `a11y.css`; vitest infra | axe: owned surfaces 0 serious/critical |
| **A7** optim | `app/optim/**` (cost_meter, cache, routing, prompt_compress, batch — all default-off); `routes/optim.py`; index migration `d9e2f4a6b8c1` | migration re-parented onto A5's by Captain (single head) |
| **A8** design | token system (`tokens.css` `--k-*`, `tailwind.config.js`), depth/material (`glass.css` `.surface*`), 3-face type; AA-contrast gate | keystone; before/after screenshots in `artifacts/agent-10/` |
| **A9** settings/icons | SF-symbol `<Icon>` system (`components/icons/**`), `lib/settings.ts`, SettingsPage | follow-up: 3 `*.test.ts` don't register under vitest |
| **A10** reading-room | `reading/ReadingRoom*.tsx`, state machine, fallback, builtin stand-ins + producer slots | designed the producer-swap seam |
| **A11** login | LoginPage, BookWall, `auth/**` (forced-colors, a11y) | uses `lucide-react` (AuthIcon) — see follow-ups |

## Follow-ups (non-blocking; logged in request queues)
- **lucide-react retained** (NOT dead): `auth/AuthIcon.tsx` (A11) imports it. To remove,
  migrate AuthIcon to A9's `<Icon>` system, then drop the dep.
- **vitest hygiene**: A9 (settings/glyphs/symbol), A10 (crossfade/fallback/machine), A2
  (timeline) wrote `*.test.ts` with `node:test`/top-level style → "No test suite found"
  under vitest. 92 vitest tests pass; these need `describe/it` or exclusion. Not in the gate.
- **A3 http**: converge `lib/api/films.ts` onto the shared `http` from `../api` (drop `lib/api/http.ts`).
- **BookOpenTransition**: A4's shelf→center travel can wrap at HomePage to complement A10's
  in-room reveal (A10 kept builtin reveal — option a).

## Recommended squash / merge plan → `main` (operator to review)
**Do NOT push to `main` without operator confirmation.** Suggested:
1. Review `overnight/integration` (this branch) — green, all 11 integrated.
2. Squash-merge to `main` grouped by lane for a readable history, e.g.:
   `feat(design-system)`, `feat(motion)`, `feat(a11y)`, `feat(icons+settings)`,
   `feat(render/event-director)`, `feat(film-api)`, `feat(scroll-film)`,
   `feat(library+covers)`, `feat(reading-room)`, `feat(login)`, `feat(optim)`,
   plus `chore(integration): rails (styles split, api primitives, coordination)`.
   — OR a single squash `feat: engagement overhaul (11-agent overnight build)` if a flat
   history is preferred. Migrations must land in order: `e843aa7682b2` → `d9e2f4a6b8c1`.
3. After merge: `make app-install && make app-desktop-dev`; backend via `make stack-up` +
   `make seed-demo`. Then prune agent worktrees: `git worktree remove ../kinora-aNN`.

_See MERGE-LOG.md for the ordered, gated merge ledger; CAPTAIN-PLAYBOOK.md for the
integration approach._
