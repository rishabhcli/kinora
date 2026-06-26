# Kinora Fleet — CONTRACTS

> Append-only. Each agent publishes the stable interfaces it owns so siblings can
> build against them without reading each other's diffs.

---

## Agent 08 — DESIGN TOKENS (color · depth · typography)

**Source of truth:** `apps/desktop/src/styles/tokens.css` (the `--k-*` custom
properties) mirrored into Tailwind via `apps/desktop/tailwind.config.js`.

### THE RULE (fleet-wide)
**No agent writes raw hex/rgb outside `tokens.css`.** Everyone consumes tokens —
either the Tailwind classes below or the `var(--k-*)` custom properties in plain
CSS. This is what lets the whole app re-skin from one file. New surfaces must use
a semantic token, never a literal colour.

### Color tokens
Stored as **space-separated RGB triples** (`--k-*-rgb`) so Tailwind `/<alpha>`
opacity modifiers work, plus `--k-*` convenience solids for plain CSS.

| Semantic | CSS var (solid) | RGB triple var | Tailwind class | Notes |
|---|---|---|---|---|
| App canvas | `--k-bg` | `--k-bg-rgb` | `bg-bg` / `bg-kinora-bg` | warm graphite |
| Deepest bg | `--k-bg-deep` | `--k-bg-deep-rgb` | `bg-bg-deep` / `bg-kinora-bg-deep` | behind everything |
| Surface | `--k-surface` | `--k-surface-rgb` | `bg-surface` / `bg-kinora-surface` | resting panels/cards |
| Surface raised | `--k-surface-raised` | `--k-surface-raised-rgb` | `bg-surface-raised` | popovers/raised |
| Surface high | `--k-surface-high` | `--k-surface-high-rgb` | `bg-surface-high` | menus/sheets |
| Text primary | `--k-text` | `--k-text-rgb` | `text-text` / `text-kinora-text` | 15.1:1 on bg |
| Text muted | `--k-text-muted` | `--k-muted-rgb` | `text-muted` / `text-kinora-muted` | 7.8:1 on bg |
| Text subtle | `--k-text-subtle` | `--k-subtle-rgb` | `text-subtle` / `text-kinora-subtle` | 5.1:1 on bg |
| Faint (deco) | — | `--k-faint-rgb` | `text-faint` | NOT for text |
| Accent | `--k-accent` | `--k-accent-rgb` | `text-accent` / `*-kinora-gold` | "lumen" gold |
| Accent strong | `--k-accent-strong` | `--k-accent-strong-rgb` | `*-accent-strong` / `*-kinora-gold-light` | bright glint |
| Accent deep | `--k-accent-deep` | `--k-accent-deep-rgb` | `*-accent-deep` | ember fills |
| Accent cool | `--k-accent-cool` | `--k-accent-cool-rgb` | `*-accent-cool` | cinema-teal |
| Success | — | `--k-success-rgb` | `*-success` | |
| Warning | — | `--k-warning-rgb` | `*-warning` | |
| Danger | — | `--k-danger-rgb` | `*-danger` | |
| Info | — | `--k-info-rgb` | `*-info` | |
| Hairline | `--k-border` | — | `border-hairline` | text @ 10% |
| Hairline strong | `--k-border-strong` | — | `border-hairline-strong` | text @ 18% |

Material fills: `--k-surface-glass`, `--k-surface-glass-strong`, `--k-scrim`,
`--k-specular`, `--k-specular-soft`.

**Legacy aliases kept working** (no broken styles): every `*-kinora-bg`,
`*-kinora-bg-deep`, `*-kinora-text`, `*-kinora-muted`, `*-kinora-subtle`,
`*-kinora-gold`, `*-kinora-gold-light` (including opacity modifiers like
`text-kinora-text/85`, `bg-kinora-gold/50`) resolves to the new tokens.

### Depth / material
- Elevation ladder: `--k-elev-1` … `--k-elev-5` → Tailwind `shadow-elev-1..5`.
  Plus `--k-ring-top` (specular rim), `--k-ring-edge`, `--k-glow-accent` →
  `shadow-ring-top`, `shadow-glow`.
- Blur: `--k-blur-sm|-|-lg|-xl` → `backdrop-blur-k-sm|-k|-k-lg|-k-xl`; `--k-saturate`.
- Material classes (in `glass.css`): **`.surface`**, **`.surface-raised`**,
  **`.surface-frosted`** (the new primitives — prefer these), plus token-driven
  re-skins of the existing `.kinora-bg`, `.glass-card`, `.glass-input`,
  `.liquid-glass-dock`, `.footer-glass`. Frosted material degrades to solid under
  `prefers-reduced-transparency`. **Never call it Liquid Glass** (native shell only).

### Typography
- Faces: `--k-font-ui` (system-first / SF Pro), `--k-font-display` (Fraunces),
  `--k-font-reading` (Newsreader), `--k-font-mono`. Tailwind: `font-ui`,
  `font-display`, `font-reading`, `font-mono`; `font-sans`→UI, `font-serif`→display.
- Scale: `--k-text-xs … --k-text-5xl` → `text-k-xs … text-k-5xl`.
- Weights `--k-weight-*`; leading `--k-leading-*`; tracking `--k-tracking-*`
  (→ `tracking-k-display|-k-tight|-k-wide|-k-caps`); measure `--k-measure(-narrow|-wide)`.
- Helpers (base.css): `.font-display`, `.font-reading`, `.text-eyebrow`,
  `.prose-reading`, `.nums-tabular`, `.scrollbar-slim`.

### Theme sets (Agent 06 binds these to the reading pane / a11y toggle)
Reading themes are values, not a global app swap — supplied as tokens:
`--k-read-dark-*`, `--k-read-night-*`, `--k-read-sepia-*`, `--k-read-paper-*`,
`--k-read-contrast-*` (each `-bg`, `-ink` as RGB triple, `-swatch`). High-contrast
overrides bind via `[data-contrast="high"]` on `<html>`. See
`requests/agent-06-from-08.md` for the recommended `READING_THEMES` values.

### What Agent 08 consumes
Nothing. (Coordinates AA / a11y with Agent 06; coordinates the `index.css` split +
font/dep tooling with Agent 12 — see `requests/agent-12-from-08.md`.)
