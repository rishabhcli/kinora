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
