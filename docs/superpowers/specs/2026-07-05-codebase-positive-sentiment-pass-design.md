# Codebase positive-sentiment / hype pass

**Date:** 2026-07-05
**Status:** approved, in execution

## Goal

Reword existing code comments and markdown documentation across the repo to
carry more confident, positive energy — subtle in code, exuberant in docs.
This is a tone/voice pass only. No user-facing surface changes at all.

## Scope

**IN:**
- Existing `#` / `//` / `/* */` comments and internal (non-API-surfaced)
  docstrings in every source file across `backend/app/**` (including
  `backend/tests/**`), `apps/desktop/src/**`, `apps/desktop-native/**`,
  `clients/**`.
- Prose in tracked markdown files repo-wide, with the exclusions below.
- Layers on top of the already-in-progress, uncommitted de-hackathon-language
  diff in the working tree — not reverting it.

**OUT (hard constraints):**
- Anything a user or API consumer could ever see: UI copy, toasts, error/log
  message strings, CLI output, JSX/markup, aria-labels, alt text, and any
  FastAPI route docstring/`summary=`/`description=` that could surface via
  OpenAPI at `/docs`.
- No new comments invented where none exist — only re-tone what's already
  there.
- No logic changes, no renames, no reformatting, no test-behavior changes.
- No frontend visual/appearance changes.
- Markdown heading text and anchors left untouched (several docs, e.g.
  `kinora.md`, have a table of contents that links `#section-slug` —
  reworded prose must not break those links). Only body prose is reworded.
- `coordination/**`, `agent-prompts/**`, `qa-runs/**` — excluded. These are
  live protocol/status/ownership files and agent mission prompts for this
  repo's parallel multi-agent build process, not narrative documentation;
  rewording them risks corrupting a parsing contract another running process
  depends on.

## Voice guide

- **Code comments — subtle.** Warmer, more confident verbs; drop hedges
  ("just", "for now", "unglamorous", "hacky", "kludge") where they undersell
  something that actually works. No exclamation points, no emoji. Preserve
  every technical fact and `§` section citation exactly.
  Example: `# drains the Redis priority queue` →
  `# keeps the render lane moving — drains the Redis priority queue`
- **Markdown docs — exuberant.** More energy in framing/intro prose,
  confident and factually-grounded superlatives, occasional "!" where
  earned. No emoji (matches current doc style). Every fact, table, code
  block, and link stays intact — only connective prose gets warmer.

## Execution

Given the file volume (~2,400 non-test + ~1,100 test source files, ~165
in-scope markdown files), this runs as a wave of parallel subagents batched
by subsystem directory, each given this spec's voice guide and hard
constraints, each free to skip any file that has no existing comments.

## Verification

- A diff filtered to non-comment/non-markdown lines should come back empty.
- `make lint` and `pnpm --filter @kinora/desktop run typecheck` to catch any
  docstring/comment-syntax breakage.
- Grep the FastAPI route files to confirm no route docstring/`summary=`/
  `description=` text changed.

No commit at the end of the pass — changes stay in the working tree.
