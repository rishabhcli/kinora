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


---

# Audit findings for Agent 9 (Settings / SF Symbols / nav) — from Agent 06 (a11y)

### 1. Active nav item needs `aria-current="page"`
`components/Navbar.tsx` (and `FloatingDock.tsx`) render tabs as `<button>`s with no
state telling SR users which page is active. Add `aria-current="page"` to the active
tab. (Icon buttons already have `aria-label`s — good, e.g. `Navbar.tsx:206`.)

### 2. Profile dropdown — menu semantics + keyboard
The profile dropdown (`Navbar.tsx:215-285`) opens on click and closes only on
outside-click/toggle. It needs: `Escape` to close + return focus to the trigger;
focus moved into the menu on open; arrow-key navigation OR plain buttons with a focus
trap. Reuse `trapFocus`/`restoreFocus` from `@/a11y/focus`. `aria-expanded` on the
trigger; `role="menu"`/`menuitem` only if you implement the full menu keyboard pattern
— otherwise keep them buttons (still needs Escape + focus return).

### 3. Settings page — labels + an Accessibility section
Ensure every Settings control has an associated `<label>`. Consider a dedicated
**Accessibility** section mirroring ReadingControls’ toggles (they’re app-global):
```ts
import { useReducedMotionPref, setReducedMotionOverride } from "@/a11y/useReducedMotionPref";
import {
  useHighContrastPref, setHighContrastOverride,
  useReducedTransparencyPref, setReducedTransparencyOverride,
} from "@/a11y/displayPrefs";
```
Render each as a `role="switch"` checkbox (see ReadingControls `Switch`). Also surface
the `?` keyboard cheat-sheet (a “Keyboard shortcuts” entry that dispatches `?`).

Verify with `pnpm --filter @kinora/desktop test:a11y` after wiring.
