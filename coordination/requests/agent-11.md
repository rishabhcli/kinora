# Cross-seam requests — Agent 11 (Login) → Agent 12 (Integration)

## 1. `index.css` aggregator must import `styles/login.css`  ✅ done in-branch, please reconcile
I migrated all login/auth + `BookWall` styles out of `apps/desktop/src/index.css` into the new
`apps/desktop/src/styles/login.css` (my lane). To keep my branch building, I made the **minimal**
seam edit myself:
- Added `@import "./styles/login.css";` near the top of `index.css` (after the `@tailwind` lines).
- **Removed** the now-migrated blocks from `index.css`: the `.bookwall*/.shelf*/.wallbook*` block and
  the `.login-*` block (the dead `.login-aurora*/.login-netflix-bg/.login-grid/.login-hero/`
  `.aurora-vignette/.login-grain/.login-card/.arrow-pulse/.aperture*` rules were unused by any
  component — removed, not migrated).
- **Preserved in `index.css`:** `.kinora-bg` (global), `.glass-*` (app-wide), and `.hero-fade-in /
  .hero-slide-up` (used by `HomePage`'s `HeroBanner.tsx`, NOT mine).

If your `index.css` split assigns the login partial differently, this is a clean drop-in: the login
partial is fully self-contained in `styles/login.css`.

## 2. Real imports to swap at integration (all isolated behind `src/components/auth/`)
- `<Icon>` (Agent 9): social + field glyphs currently render via a local `AuthIcon` shim. Swap to the
  shared icon component; names used: `google`, `apple`, `github`, `mail`, `lock`, `eye`, `eye-off`,
  `check`, `arrow-right`.
- a11y primitives (Agent 6): the form already meets the a11y contract locally (labels, `aria-live`
  errors, focus-visible, reduced-motion). If Agent 6 ships shared field/announcer primitives, they can
  replace the local `auth/Field` + `auth/useAnnouncer` without markup changes.
- motion primitives (Agent 4): the enter transition + reveals use local helpers honoring the
  opacity-only-home invariant (see STATUS.md). Swap to `<BookOpenTransition>/<Reveal>` if/when they
  preserve that invariant.
- cover API (Agent 5): `auth/coverCache.getCoverUrls()` currently sources cover URLs from local
  `data/books`. Point it at Agent 5's HD cover/thumbnail API when available.

## 3. No dependency / lockfile changes
TDD for pure functions runs via `node --test` (Node 26 strips `.ts`) under
`apps/desktop/tests/auth/` — **no new devDeps, no lockfile churn.**

## 4. Post-integration cleanup notes (from code review — not blocking)
- **Dust-mote duplication:** `auth/AmbientBackdrop` + `login.css .auth-mote` re-implement the same
  drifting-dust technique as the home `AmbientBackground.tsx` + `index.css .ambient-mote`. This is a
  consequence of the lane split (login styles are required to be self-contained in `login.css`).
  Post-integration, Agents 10/12 could factor a shared mote primitive + keyframe and have both
  backdrops consume it. Left duplicated on purpose to avoid editing the home component / `index.css`
  rules I don't own.
- **`EASE` curve `[0.22,1,0.36,1]`** is inlined in `App.tsx` + `LoginPage.tsx` (matches the repo's
  existing house style — same literal in BookShelf/AnimatedPageSwitch/PricingPage/ReadingRoom). When
  Agent 4 ships `src/motion/`, a single exported easing const can replace all copies.
