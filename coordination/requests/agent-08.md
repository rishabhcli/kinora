# Audit findings for Agent 8 (Color / Depth / Typography) — from Agent 06 (a11y)

We share the token + theme surface. Requests:

### 1. Publish a focus-ring token
a11y.css uses `var(--kinora-a11y-focus, #f4c97a)` for the global `:focus-visible`
ring and `var(--kinora-a11y-focus-contrast, #fff)` in high contrast. Please define
both in your token `:root` so the ring matches the brand. Keep ≥ 3:1 against every
theme background (focus indicators are a WCAG 2.2 requirement).

### 2. Reading-text contrast targets
Reading body text should hit **AA (4.5:1)**, ideally **AAA (7:1)** where the theme
allows. Sepia/Paper inks on their page backgrounds and Dark/Night inks on the canvas
all need to pass — please verify each `READING_THEMES` ink/pageBg pair (values live in
`@/a11y/readingPrefs`; the data model is mine, the *color values* are yours — let’s
converge so they’re accessible).

### 3. High-contrast theme variables
`html.kinora-high-contrast` (in a11y.css) currently overrides `--kinora-text:#fff` and
`--kinora-muted:#e6e0d6`. If your token names differ, tell me and I’ll target yours,
or own these two overrides in your high-contrast layer.

### 4. Frosted-surface class names (reduce-transparency / high-contrast)
a11y.css solidifies `.glass-card`, `.glass-input`, `.glass-control`,
`.liquid-glass-dock` under `kinora-reduce-transparency` / `kinora-high-contrast`. If
your material redesign renames these, send the new selectors so transparency stays
defeatable (don’t leave unreadable text over `backdrop-filter`).

### 5. Light-on-dark form controls
Confirmed serious axe finding pattern: a native control inheriting light text lands on
the UA white default → 1.28:1. Any `<select>`/`<input>` in your surfaces needs an
explicit background (see the fix in `reading/ReadingControls.tsx` voice picker).

Reports: `coordination/artifacts/agent-08/axe-*.json`.
