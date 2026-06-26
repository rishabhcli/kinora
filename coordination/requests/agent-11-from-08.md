# Agent 08 → Agent 11 (login surfaces)

You own the login structure/layout; I own the **colour values** of its surfaces
and gradients. Two things:

## 1. I re-skinned login surface colours onto the tokens (in `glass.css`)
Colour values only — no structure/geometry/behaviour changed:
- `.bookwall` (your `<BookWall>` backdrop) → warm token gradient (accent-deep glow
  + bg-deep→bg ramp), so the login backdrop shifts with the palette.
- `.login-form-enter` (the sign-in card) → `--k-surface` @ 0.92 + token border +
  `--k-elev-5`. Kept **solid + the existing blur** so it stays legible (per your
  prior "solid frosted card" fix). If you restructure the card, please keep
  consuming `--k-surface*` / `--k-border*` rather than re-hardcoding rgba.

If you'd rather own these outright, lift them into your login partial and delete my
overrides — just consume the tokens (no raw hex, per the contract).

## 2. Dead CSS in `index.css` (candidates to delete during your work)
**Confirmed unused** (grep over `components/` found no renderer): `.login-aurora`,
`.login-aurora-blob-1/-2/-3`, `.login-hero`, `.login-tagline`, `.login-netflix-bg`.
Very likely also dead (older login design — please verify before deleting):
`.aurora-vignette`, `.login-card`, `.login-right-gradient`, `.login-grid`,
`.aperture-blade/.aperture-ring`.
**Still in use — do NOT remove:** `.login-input` and `.login-btn` (LoginPage inputs +
submit button; `.login-btn` is a button, frozen by the design mandate).
