# Cross-seam requests — Agent 10 (reading-room shell)

Agent 12 applies these centrally (shared seams: `lib/api.ts`, the `ReadingRoom`
re-export shim, `main.tsx`, `package.json`).

## 1. `apps/desktop/src/components/ReadingRoom.tsx` → re-export shim
Per mission, the implementation moves to `apps/desktop/src/reading/ReadingRoom.tsx`.
I leave `components/ReadingRoom.tsx` as a one-line re-export so `HomePage.tsx`
(Agent 4) keeps resolving `import ReadingRoom from "./ReadingRoom"` unchanged:

```ts
export { default } from "../reading/ReadingRoom";
```

Status: **done on this branch** (shim committed). Flagging because the shim file
is on the shared-seam list — verify no conflict with Agent 4's `HomePage.tsx`.

## 2. Wire the real producer components (integration swap)
`apps/desktop/src/reading/producers.tsx` currently exports built-in stand-ins so
the room is fully functional standalone. At integration, swap each to the real
component (identical prop contracts — see `coordination/CONTRACTS.md`):

- `ScrollFilmEngine` → Agent 2 `src/reading/ScrollFilmEngine.tsx`
- `ReadingControls` → Agent 6 `src/reading/ReadingControls.tsx`
- `BookOpenTransition` → Agent 4 `src/motion/BookOpenTransition.tsx`

Only `producers.tsx` changes (3 import lines). Nothing else in my lane needs edits.

## 3. (Optional, CI parity) Add vitest to `apps/desktop`
Pure logic is currently TDD'd with Node 26 native type-stripping + `node --test`
(no dep needed; runs in this repo today). For monorepo CI parity, optionally add:

```jsonc
// apps/desktop/package.json
"devDependencies": { "vitest": "^2.1.0" },
"scripts": { "test": "vitest run" }
```

My `*.test.ts` files use only `node:test` + `node:assert/strict`, so they keep
working with or without vitest. Not required for the DoD (`typecheck && build`).
