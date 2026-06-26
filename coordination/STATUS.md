# Kinora overnight build — STATUS

## Agent 04 — Motion & Animation system  ·  ✅ COMPLETE

Branch `agent/04-motion` (worktree `../kinora-a04`, base `main` —
`overnight/integration` not yet created by Agent 12).

**Delivered**
- `src/motion/` — the app-wide motion vocabulary (published in CONTRACTS.md):
  springs/ease/duration tokens (speed-scalable, reduced-aware), `MotionProvider`
  + `useMotion`, `Reveal`, `PageTransition`, `BookOpenTransition` (shared-element
  morph), `ShelfScroller` (inertial + parallax + snap + edge DoF), `Tilt`/
  `useTilt`, `Pressable`, `MotionDebugOverlay`, `useSharedElement`, `variants`.
- `src/styles/motion.css` — motion tokens, consolidated keyframes (migrated
  `dropdownIn` → `mo-dropdown-in`), transition utilities, reduced-motion +
  reduced-transparency law.
- **Wired the shell:** HomePage mounts `MotionProvider` + `MotionDebugOverlay`,
  uses `PageTransition` (replaced `AnimatedPageSwitch` → deprecated shim),
  `Reveal` (shelf stagger), and `BookOpenTransition` around the reading-room
  mount (with `.book-cover` rect capture, no BookCard edit). Navbar consumes
  `useMotion` (pill spring, dropdown, tokenized chrome). FloatingDock is
  reduced-motion aware.
- **Three signature moments demonstrated smooth** (Chrome for Testing capture):
  open ~12 ms/frame · close ~13 ms · shelf ~8.3 ms, 0–2 long frames — well
  inside the 60 fps (16.67 ms) budget. Stills + videos + `fps-report.json` in
  `coordination/artifacts/agent-06/` (incl. reduced-motion degradation).
- `pnpm --filter @kinora/desktop typecheck && build` green.

**Seams (stubbed; see `requests/agent-04.md`)**
- Reduced-motion: own stub wraps framer-motion `useReducedMotion`; Agent 12 to
  repoint at Agent 6's `src/a11y/useReducedMotionPref`.
- `MotionProvider` mounted in HomePage; asked Agent 12 to also wrap `<App>`
  (login screen).
- `ShelfScroller`/`Tilt`/`Reveal` published for Agent 5 to wrap real book rows.
- vitest requested (package.json is a shared seam) for motion pure-fn coverage.

**Notes**
- Motion showcase gated behind `?motiondemo` (never user-facing) — demonstrates
  ShelfScroller/Tilt before Agent 5 wiring.
- Did NOT touch index.css (Agent 8/12's split), tailwind.config.js, or any
  sibling-agent files. Stretch debug overlay (⌥⇧M) shipped.
