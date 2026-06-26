# MERGE LOG

Every merge into `overnight/integration`, in order, with the gate result.
Captain-only file.

| # | When | What | Commit(s) | Gate | Notes |
|---|---|---|---|---|---|
| 0 | 2026-06-26 | Branch `overnight/integration` from `main` | `4863a0c` | n/a | base |
| 1 | 2026-06-26 | Captain baseline: agent mission infra + gitignore `.claude/` | `b4bcdb3` | n/a | infra only |
| 2 | 2026-06-26 | Adopt operator pre-staged baseline (A5 seeds/epubs, A8 tailwind tweak) | `80eb58c` | n/a | attributed; not Captain-authored |
| 3 | 2026-06-26 | t0 scaffolding (coordination/) | `1db66fd` | n/a | docs |
| 4 | 2026-06-26 | Seam: `api.ts` BASE/http primitives (+ index.css rm) | `3ef62a3` | green | CONTRACTS §7 |
| 5 | 2026-06-26 | Seam: split `index.css` → `styles/` partials + postcss-import aggregator | `31326f8` | green | tc+build; CSS 23→44 kB (verified custom classes present) |

## Agent merges (dependency order: A8 → A6 → A4 → A9 → A1 → A3 → A2 → A5 → A10 → A11 → A7)
| # | When | Agent | Merge commit | Conflicts resolved | Gate |
|---|---|---|---|---|---|
| 6 | 2026-06-26 | **A9** settings/icons | `7d5afad` | none (additive) | tc+build green |
| 7 | 2026-06-26 | **A4** motion | `c4b13c1` | `styles/motion.css` (concat) | tc+build green |
| 8 | 2026-06-26 | **A8** design tokens (keystone) | `d7825ea` | tokens/base/glass.css (concat), main.tsx (ours), tailwind (theirs), CONTRACTS (fold) | tc+build green; CSS→57 kB |
| 9 | 2026-06-26 | **A6** a11y | `a123564` | a11y.css (concat), main.tsx (`<A11yProvider>`+aggregator), pkg.json (deps union), CONTRACTS/requests | tc+build green; lockfile regen |
| 10 | 2026-06-26 | **A1** event-director (backend) | `b2f9709` | none (disjoint) | `make test` 325 pass / 125 skip / 0 fail |
| 11 | 2026-06-26 | **A7** optim (backend) | `7614f84` | coordination only | `make test` 384 pass / 0 fail |
| 12 | 2026-06-26 | **A10** reading-room | `20a359e` | coordination only | tc+build green |
| 13 | 2026-06-26 | **A2** scroll-film | `49b6cf6` | pkg.json scripts union; **Captain fixed A2 `__demo__` ReadingPrefs drift** (a11y contract grew) | tc+build green |
| 14 | 2026-06-26 | **A3** film-api | `fb7d6d0` | coordination; **Captain registered `films.router`** | tc+build + `make test` 391 pass |
| 15 | 2026-06-26 | **A5** library | `3190158` | **Captain unioned ROUTERS (films+library)**; alembic single head `e843aa7682b2` | tc+build + `make test` 408 pass |
| 16 | 2026-06-26 | **A11** login | `aa2b406` | `index.css` kept deleted (A11 also split login out); `login.css` concat | tc+build green; CSS→73 kB |

### 🏁 Cycle 1 complete — ALL 11 agents first-merged; full gate GREEN
`tc+build` green · `make test` 408/0 · vitest 64 pass (7 files non-vitest, tracked).

**Pending re-merges** (agents advanced after first merge): A1+4, A3+1, A4+3, A6+5, A7+1, A8+1, A9+3.
A2/A5/A10/A11 current (0 pending). Re-merge sweep is the next cycle (git rerere auto-applies seam resolutions).

## Cycle 2 — re-merge sweep (deterministic, per CAPTAIN-PLAYBOOK)
| # | Agent | Merge commit | Captain resolution | Gate |
|---|---|---|---|---|
| 17 | A8 +1 | `02f344a` | glass.css regen; **+10 DoD#2 before/after screenshots** | tc+build green |
| 18 | A6 +6 | `3bc2014` | a11y.css regen; requests **unioned** (kept vitest note + A6's A9 audit) | tc+build green |
| 19 | A4 +5 | `c1a81a6` | motion.css regen | tc+build green |
| 20 | A9 +3 | `43a5903` | coordination ours | tc+build; vitest 92 pass |
| 21 | A1 +4 | `7806c89` | test_api_director → typed `PubSub` (imported) | `make test` 408 |
| 22 | A3 +1 | `b6f6353` | coordination ours | tc+build + `make test` 408 |
| 23 | A2 +2 | `381debd` | coordination ours; demo prefs fix retained | tc+build green |
| 24 | A5 +2 | `7575eb0` | seed_* → theirs (A5 lane); single head | `make test` 408 |
| 25 | A10 +1 | `5875b9b` | reading.css regen | tc+build green |
| 26 | A11 +4 | `4bf3033` | login.css regen; index.css stays deleted | tc+build green |
| 27 | A7 +2 | `fcffae1` | **re-parented migration `d9e2f4a6b8c1`→`e843aa7682b2`** (collapsed 2-head fork); `__init__.py` ours (films+library+optim) | `make test` 408 / **single head** |

### 🏁 Cycle 2 complete — all 11 re-merged to latest; gate GREEN (tc+build + make test 408/0).
Residual pending (agents committed during the sweep): A6+4, A5+1, A7+1, A10+1 → next cycle.
**Captain helper:** `scratchpad/remerge.sh` (deterministic CSS + coordination policy + requests union).
