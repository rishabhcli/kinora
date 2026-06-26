# Kinora Overnight Build — STATUS

Shared status board. Each agent owns its own section; append, don't overwrite others.

---

## Agent 11 — Login Experience
**Branch:** `agent/11-login` · **Worktree:** `../kinora-a11` · **Base:** `overnight/integration`

**State:** all four workstreams implemented + verified; polishing.

### Done
- Worktree + branch bootstrapped; baseline + current `typecheck` green; `build` green; 16/16 unit
  tests (node --test).
- Design plan locked (see `coordination/artifacts/agent-11/DESIGN-NOTES.md`): "private screening
  room for literature" — projector beam + dust signature, warm-theatre/brass/cream tokens.
- **WS1** cinematic backdrop: depth-parallax `BookWall` subdued into a moody room + warm projector
  beam + drifting dust + vignette (`AmbientBackdrop`), per-launch variant. transform/opacity only,
  reduced-motion static fallback, native-vibrancy aware.
- **WS2** form perfected: `auth/` kit (Field, PasswordField + show/hide + strength meter, SocialRow,
  AuthIcon). Friendly real-time validation + aria-live announcer, submit idle/loading/success/error,
  Sign In↔Sign Up morph, remember/forgot, demo entry. AA contrast (16.4/8.1:1), full keyboard order.
- **WS3** enter transition: card recedes + wall blooms → warm flash → home cross-fades in
  (opacity-only home wrapper → fixed navbar anchor preserved). Reduced-motion: clean cross-fade.
- **WS4** branding: Fraunces lockup + "Now showing" eyebrow + cold-launch warm-up (once/session).
- Login + BookWall styles **migrated** to `styles/login.css` (token-driven `--auth-*`); index.css
  `@import`s it. Artifacts (screens + transition video + a11y report) in `artifacts/agent-11/`.

### Contract notes (for Agents 4/6/8/9/12)
- **App.tsx enter-transition contract:** login→home hand-off keeps the home wrapper **opacity-only**
  (no transform/filter/backdrop-filter on the wrapper) so `HomePage`'s fixed navbar keeps its anchor.
  The cinematic motion lives on the *login* exit (card recedes + wall bloom). If Agent 4 supplies a
  shared enter primitive, it must preserve this invariant.
- **Tokens:** login screen reads `--auth-*` CSS vars defined at the top of `styles/login.css`. Agent 8
  can repoint these to global `--kinora-*` tokens without touching markup.
- **Stubs awaiting real imports (Agent 12 to swap at integration):** `<Icon>` (Agent 9) for
  social/field glyphs, a11y primitives (Agent 6), motion primitives (Agent 4), Agent 5 cover API as
  the source for `coverPrefetchList`. All are isolated behind local modules under
  `src/components/auth/`.

### Requests filed
- See `coordination/requests/agent-11.md` (index.css aggregator must `@import "./styles/login.css"`).
