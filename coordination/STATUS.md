# STATUS — overnight/integration live board

**Captain:** Agent 12 · **Branch:** `overnight/integration` · **Base:** `4863a0c` (main)
**Last updated:** 2026-06-26 (Captain iteration — rails build)

## 🚦 GATE: setting up (rails not yet committed)
Runnable gate (per CLAUDE.md current reality — `packages/core`/`apps/mobile` don't exist):
```
pnpm install && pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/desktop build
```
Backend gate (when backend-owned branches merge): `make lint && make test`

## 📣 ANNOUNCEMENT
**Status: RAILS IN PROGRESS — not yet GO.** Worktrees exist for all 11 agents at the
baseline. The Captain is laying the t0 rails (CSS partials, `api.ts` primitives,
coordination docs). **GO** will be announced here once rails are committed and the gate is
green; at that point each agent branch is fast-forwarded onto `overnight/integration` so
everyone starts with the rails. Until then: read your mission + `OWNERSHIP.md` + `CONTRACTS.md`,
and code against the contracts (stub absent producers).

## Per-agent board
| Agent | Lane | Branch | Worktree | Commits | Merged? | Notes |
|---|---|---|---|---|---|---|
| A1 | event-director / stitch | `agent/01-event-director` | `../kinora-a01` | 0 | — | baseline |
| A2 | scroll-film engine | `agent/02-scroll-film` | `../kinora-a02` | 0 | — | baseline |
| A3 | film API + sync | `agent/03-film-api` | `../kinora-a03` | 0 | — | baseline |
| A4 | motion / animation | `agent/04-motion` | `../kinora-a04` | 0 | — | baseline |
| A5 | library / books / epub | `agent/05-library` | `../kinora-a05` | 0 | — | inherits operator public-domain seeds |
| A6 | accessibility | `agent/06-a11y` | `../kinora-a06` | 0 | — | baseline |
| A7 | optimization | `agent/07-optim` | `../kinora-a07` | 0 | — | merges LAST |
| A8 | color/depth/typography | `agent/08-design` | `../kinora-a08` | 0 | — | merges FIRST; inherits operator tailwind tweak |
| A9 | settings / SF-symbol icons | `agent/09-settings-icons` | `../kinora-a09` | 0 | — | baseline |
| A10 | book-open / film experience | `agent/10-reading-room` | `../kinora-a10` | 0 | — | baseline |
| A11 | login experience | `agent/11-login` | `../kinora-a11` | 0 | — | baseline |

## Merge order (dependency order)
`A8 → A6 → A4 → A9 → A1 → A3 → A2 → A5 → A10 → A11 → A7`

## Blocked / open requests
_None yet. File cross-seam needs in `coordination/requests/agent-12.md`._

## Captain rail checklist (t0)
- [x] `coordination/` scaffolding (OWNERSHIP, CONTRACTS, STATUS, MERGE-LOG, requests, artifacts)
- [ ] Split `index.css` → `styles/` partials + aggregator; point `main.tsx` at it
- [ ] Refactor `lib/api.ts` to export `BASE`/`auth`/`http`/`toBrowserUrl`
- [ ] Gate green on `overnight/integration`
- [ ] Fast-forward all 11 agent branches onto `overnight/integration`
- [ ] Announce **GO**
