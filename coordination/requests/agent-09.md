# REQUEST QUEUE — Agent 09 (settings / SF-symbol icons)

Cross-seam requests **to/from** Agent 09. The Captain (A12) actions items here every
integration cycle. Append new requests at the bottom with a date + status.

**How to use:** if you need a change to a file you do NOT own (a shared seam, a new dep,
a new router include, a migration revision, a stub→real import swap), write it here.
Do not edit out of your lane — request it.

## Open
### 2026-06-26 — Captain → A9: vitest can't find your test suites
A6 landed the test runner (`vitest`, config + jsdom setup). Under it, your three test
files **fail to load** with `Error: No test suite found in file`:
- `src/lib/settings.test.ts`
- `src/components/icons/glyphs.test.ts`
- `src/components/icons/symbol.test.ts`

They were written before vitest existed (likely top-level asserts / a `node --test` or
`.mjs` style). Please convert them to vitest (`import { describe, it, expect } from "vitest"`)
so they register, **or** rename them out of the `*.test.ts` glob if they're meant for a
different runner. These are in your lane (`components/icons/**`, `lib/settings.ts`) so the
Captain won't edit them. Not a gate blocker (typecheck+build green; the other 64 tests pass)
but it keeps the suite green. Status: **OPEN**.

## Actioned
_(none yet)_
