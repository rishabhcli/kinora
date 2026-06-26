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

**Pending re-merges** (agents advanced after first merge): A1 +1, A6 +2, A8 +1, A9 +3 — cheap via `git rerere`.
**Not yet merged:** A2, A3, A5, A7, A10, A11.
