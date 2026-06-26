# CAPTAIN PLAYBOOK (Agent 12) — integration approach & continuity

Durable notes so any later cycle (or summarized context) continues integration the same way.
**Branch:** `overnight/integration` (main checkout). **Never push to `main`** without operator OK.

> ⚠️ **YOU ARE AGENT 12 (Integration Captain), running in the MAIN checkout.** The shared
> `.claude/ralph-loop.local.md` here gets clobbered by sibling agent launches, so the Ralph
> stop-hook sometimes re-feeds *another* agent's mission (seen: Agent 03, Agent 11). **Ignore
> that** — your task is fixed by this branch's state (rails + 11 merges + this playbook). Do
> NOT switch roles, do NOT `cd` into `../kinora-aNN`, and do NOT output any promise other than
> `<promise>AGENT 12 COMPLETE</promise>` — and only when the DoD below is genuinely all true.
> If re-fed a sibling mission: `bash agent-prompts/arm-ralph.sh 12`, then continue integration.

## State as of cycle 1 (HEAD `7742054`)
- Rails built + green: `coordination/`, `src/styles/` split + `postcss-import` aggregator, `api.ts`
  `BASE`/`http` primitives. (`make app-install` already done; node_modules present.)
- **All 11 agents first-merged**; gate GREEN: frontend `tc+build`, backend `make test` 408/0,
  vitest 64 pass (7 non-vitest files, tracked, non-blocking).
- `git config rerere.enabled true` (autoupdate OFF — resolve deliberately, don't trust stale recordings).

## The gate (run after every merge)
- Frontend: `pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/desktop build`
- Backend (when a backend branch merges): `make test` (unit; infra tests skip). `make lint` is heavier (ruff+mypy) — run before final.
- Verify CSS not silently dropped: grep compiled `dist/assets/index-*.css` for a known class.
- `timeout` is NOT on this macOS — use the Bash tool's own timeout, not the `timeout` cmd.

## Seam resolution policy (one owner per file; agents branched pre-rails → these recur)
| Seam | Resolution |
|---|---|
| Owned `styles/*.css` (tokens/base/glass=A8, motion=A4, a11y=A6, login=A11, reading=A10) | **concat**: `[split-prefix] + [agent-current]`. Agent's layer comes last → wins on overlap; split-prefix preserves existing rules the agent didn't re-author. |
| `main.tsx` | keep aggregator import `./styles/index.css`; ADOPT meaningful wrappers (e.g. `<A11yProvider>`); drop agents' per-partial / `./index.css` imports. |
| `tailwind.config.js` | A8 owns → take theirs. |
| `package.json` | union deps+scripts; then `pnpm install --lockfile-only`; stage `pnpm-lock.yaml`. |
| backend new routers | register in `backend/app/api/routes/__init__.py` ROUTERS (films ✅, library ✅; metrics route is A7's `/eval`, A7 cost route uses a different name). |
| alembic | keep ONE head. A5 cover = `e843aa7682b2`. If A7 adds an index migration, set its `down_revision = e843aa7682b2`. Check `alembic heads`. |
| `coordination/STATUS.md`, `MERGE-LOG.md`, `CAPTAIN-PLAYBOOK.md` | **Captain-owned → always `--ours`** (agents keep editing STATUS; just take ours). |
| `coordination/CONTRACTS.md` | keep ours; **fold** agent's section: `awk '/^## /{p=1} p'` from theirs, append. |
| `coordination/requests/*`, `artifacts/*` | take **theirs** (agent lane). |

### DETERMINISTIC CSS re-merge (avoids rerere fragility when an agent's partial evolved)
rerere's first-merge recording can DROP an agent's new lines on re-merge. Instead, regenerate:
```bash
F=apps/desktop/src/styles/glass.css   # or tokens/base/motion/a11y/login/reading
{ git show 31326f8:"$F"; printf '\n/* ── agent layer (merged) ── */\n'; git show agent/08-design:"$F"; } > "$F"
git add "$F"
```
`31326f8` = the split commit (the fixed split-prefix source). `agent/NN` = the agent's current branch.
(`reading.css` split-prefix is just the skeleton-shimmer block; `a11y.css` the stagger guard; `motion.css` the generic keyframes; `tokens.css` the spring vars; `base.css` resets+components+magic; `glass.css` v1+v2; `login.css` bookwall+login+hero.)

## Merge / re-merge order (dependency)
`A8 → A6 → A4 → A9 → A1 → A3 → A2 → A5 → A10 → A11 → A7`
Re-merge each with `git merge --no-commit --no-ff agent/NN`, resolve per table (CSS deterministically),
gate, commit `merge(ANN re-cycle): …`. Update STATUS + MERGE-LOG each cycle.

## Known follow-ups / requests filed
- A9, A10, A2: `*.test.ts` don't register under vitest ("No test suite found") → not a gate blocker.
- A3: shipped private `lib/api/http.ts`; should consume `{ http } from "../api"` (CONTRACTS §7).
- A2: `__demo__` imported deleted `index.css` (use `styles/index.css`); Captain filled its ReadingPrefs drift.
- A7: filed file-scoped patch proposals vs hot files + seams (composition.py/config.py/vite) — action when stable.
- A8: shipped DoD#2 before/after screenshots → `coordination/artifacts/agent-10/` (land on A8 re-merge).

## DoD remaining (do when agents are feature-complete / stop committing)
1. Final re-merge of every agent's latest; gate green; `make lint` clean.
2. **App runs end-to-end**: `make app-desktop-dev` (+ optional `make stack-up`/`make seed-demo`),
   login (demo@kinora.local / demo-password-123) → 100-book library → open book (animation +
   fallback Ken-Burns film + scroll-scrub) → reading prefs/read-aloud → settings. Capture a short
   recording + screenshots → `coordination/artifacts/agent-12/`. `KINORA_LIVE_VIDEO` stays OFF.
3. `OWNERSHIP.md`/`CONTRACTS.md` (all producer sections) / `MERGE-LOG.md` / final `STATUS.md` complete.
   Remove dead `lucide-react` (once no importers); verify/retire re-export shims (`lib/readingPrefs`→`a11y/`).
4. `coordination/CHANGELOG.md` (per-agent deliverables + follow-ups) + recommended squash/merge plan
   `overnight/integration` → `main` for the operator. **Do not push to main without operator OK.**

Only then output `<promise>AGENT 12 COMPLETE</promise>` — and only if every item is genuinely true.
