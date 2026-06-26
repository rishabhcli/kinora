# Kinora accessibility checklist (Agent 06)

**Target: WCAG 2.2 AA** (AAA for reading-text contrast where feasible). Every PR that
touches UI must satisfy this. Import primitives from `@/a11y/*` — don't re-implement.

## Use the shared primitives (don't reinvent)
- **Motion:** consume `useReducedMotionPref()` (NOT framer-motion's `useReducedMotion()`)
  so the in-app toggle works. For imperative code use `getReducedMotionSnapshot()`.
- **SR-only text:** `<VisuallyHidden>` or the `.sr-only` class.
- **Status/announcements:** `announce(msg, "polite" | "assertive")` — never rely on a
  visual-only toast for important state (generation done, errors, saved).
- **Dialogs/popovers:** `trapFocus(el)` on open + `restoreFocus(prev)` on close.
- **Shortcuts:** `registerShortcut(combo, fn, { description, scope })` so it shows in the
  `?` cheat-sheet. Use `whenInputFocused: true` only for Escape-like keys.

## Keyboard (operable)
- [ ] Every interactive element is reachable and operable with the keyboard alone.
- [ ] Logical, source-order focus; no traps except intentional modal traps (which
      release + restore focus on close).
- [ ] `Escape` closes menus/dialogs/popovers and returns focus to the trigger.
- [ ] No keyboard-only dead-ends; custom widgets implement their ARIA keyboard pattern.

## Focus visible
- [ ] Keyboard focus is always visible. Do **not** add `outline: none` on `:focus`
      without a `:focus-visible` replacement — a11y.css provides a global ring; don't
      override it away.

## Names, roles, states
- [ ] Every control has an accessible name (visible label, `aria-label`, or
      `aria-labelledby`). Icon-only buttons MUST have a name.
- [ ] Correct roles: `role="dialog"` + `aria-modal` for modals; `aria-expanded` on
      disclosure toggles; `aria-current="page"` on the active nav item; menus use the
      menu pattern or are plain buttons (not fake menus).
- [ ] Images/decorative elements: meaningful `alt`, or `aria-hidden`/`alt=""` if decorative.
- [ ] One `<main id="kinora-main">` landmark per screen (the skip link targets it).

## Color & contrast
- [ ] Text ≥ 4.5:1 (AA), large text ≥ 3:1. Reading text aims for AAA (7:1) where the
      theme allows. UI affordances (focus ring, borders) ≥ 3:1.
- [ ] Information is never conveyed by color alone.
- [ ] Honor `prefers-contrast` / the `kinora-high-contrast` class (a11y.css).

## Motion & transparency
- [ ] All animation respects `useReducedMotionPref()`; no essential info is animation-only.
- [ ] No content flashes more than 3×/sec.
- [ ] Frosted/`backdrop-filter` surfaces stay legible under `kinora-reduce-transparency`.

## Forms & errors
- [ ] Inputs have associated `<label>`s; errors are announced (`announce(..., "assertive")`)
      and linked via `aria-describedby`.

## Verify before merge
- [ ] `pnpm --filter @kinora/desktop typecheck` green.
- [ ] `pnpm --filter @kinora/desktop test:a11y` (axe) shows **zero serious/critical** on
      your surface.
- [ ] One keyboard-only pass through your feature. One VoiceOver pass (names/roles read
      correctly, focus order sensible).
