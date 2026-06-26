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
