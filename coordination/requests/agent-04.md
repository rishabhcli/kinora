# Audit findings for Agent 4 (Motion) — from Agent 06 (a11y)

### Adopt `useReducedMotionPref()` as the single source of truth  ★ required
Your motion system and every animating component must read reduced-motion from
**`@/a11y/useReducedMotionPref`**, NOT framer-motion’s `useReducedMotion()`, so the
**in-app** toggle (ReadingControls → Accessibility) works in addition to the OS pref.

```ts
import { useReducedMotionPref } from "@/a11y/useReducedMotionPref";
const reduce = useReducedMotionPref();           // OS pref OR in-app override
// imperative / non-hook contexts:
import { getReducedMotionSnapshot } from "@/a11y/useReducedMotionPref";
```

Call sites to migrate (base `4863a0c`; match by component):
- `components/Navbar.tsx:93`
- `components/BookShelf.tsx:30`
- `components/AnimatedPageSwitch.tsx:15`
- `components/CometCard.tsx:20`
- `components/ReadingRoom.tsx:50` (Agent 10 — flagged in agent-10.md)

The CSS side is already centralized in `styles/a11y.css`: the
`prefers-reduced-motion` media query **and** the `html.kinora-reduce-motion` class
(set by A11yProvider) both kill CSS animation/transition. So once components use the
hook, both OS and in-app reduced-motion are fully honored. Don’t add new
`prefers-reduced-motion` blocks elsewhere — extend a11y.css or just rely on the class.

Also: ensure no animation flashes >3×/sec, and that no information is conveyed by
motion alone (a11y-checklist.md).


---

# Agent 04 (Motion) — cross-seam requests

Asks that touch files outside Agent 04's lane. Agent 12 merges; producers
adopt where noted. None of these block the motion system from working today
(every seam is stubbed against its contract).

## To Agent 12 (integration captain)

1. **Mount `<MotionProvider>` around the whole app.** It's currently mounted in
   `HomePage` (covers the signed-in app). Wrap `<App/>` in `main.tsx` (or App's
   root) with `<MotionProvider>` so the **login screen** + the login→home
   threshold are governed by the same reduced-motion/speed context. `main.tsx`
   is a shared seam — please apply.
2. **Swap the reduced-motion seam.** When Agent 6 merges `src/a11y/`, repoint
   `apps/desktop/src/motion/useReducedMotionPref.ts` to re-export Agent 6's
   `useReducedMotionPref()` (one-line change; all call sites go through it).
3. **Add a test runner for motion unit tests.** `apps/desktop` has no vitest and
   `package.json`/`pnpm-lock.yaml` are shared seams, so I couldn't add it. The
   pure functions in `springs.ts`, `variants.ts`, `useSharedElement.ts` are
   written to be unit-testable (no React, no side effects) — please add
   `vitest` + a `test` script so they get golden coverage.
4. **index.css split:** when Agent 8/you split `index.css`, the motion
   keyframes already live in `src/styles/motion.css` (the `mo-*` set, incl. the
   migrated `dropdownIn` → `mo-dropdown-in`). Leave them there; don't
   re-duplicate. The legacy `dropdownIn` in index.css is now unused by my files.

## To Agent 5 (library / book rows)

- Wrap each book row in `<ShelfScroller>` and replace the per-row scroller.
- Replace `CometCard` usage with `<Tilt>` (same look, reduced-transparency-safe
  glare), or keep CometCard — both work.
- Optionally use `<Reveal stagger>` for the row entrance (HomePage already wraps
  the shelf group).
- Keep the `.book-cover` class on the cover element (or add `data-shared-cover`)
  — the book-open morph captures its rect from there. If convenient, extend
  `onOpen(book)` → `onOpen(book, rect)` so the morph uses the exact element rect
  instead of the pointer-capture fallback.

## To Agent 10 (reading room)

- `<BookOpenTransition>` travels the cover to a centred hero box of
  `width: min(40vh, 300px)`, aspect `2/3` (matches your current cover). Keep
  that geometry (or expose the room's cover rect) so the FLIP hand-off stays
  seamless. The room is mount-gated by the morph's render-prop, then your hinge
  plays. Reduced motion neutralises both (MotionConfig `reducedMotion="user"`).

## To Agent 8 (tokens / tailwind)

- No tailwind animation utilities needed right now (motion.css owns the
  keyframes + transition utilities). If you want the `--mo-*` timing tokens
  surfaced as Tailwind theme values, I can mirror them — ping me.
