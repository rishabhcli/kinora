# Kinora Overnight Build — STATUS

Shared status board. Each agent owns its own section; append, don't overwrite others.

---

## Agent 11 — Login Experience
**Branch:** `agent/11-login` · **Worktree:** `../kinora-a11` · **Base:** `overnight/integration`

**State:** in progress.

### Done
- Worktree + branch bootstrapped; baseline `typecheck` green.
- Design plan locked (see `coordination/artifacts/agent-11/DESIGN-NOTES.md`): "private screening
  room for literature" — projector beam + dust signature, warm-theatre/brass/cream tokens.

### In progress
- WS1 cinematic backdrop · WS2 form perfected · WS3 enter transition · WS4 branding/first-5s.
- Migrating login + BookWall styles → `styles/login.css` (token-driven).

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
