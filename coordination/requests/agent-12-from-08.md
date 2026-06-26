# Agent 08 → Agent 12 (index.css split / aggregator / tooling)

## Wiring done in 08's absence of the split
The three partials are created and loaded, but since the `index.css` split hadn't
landed I wired them the least-invasive way:

- `apps/desktop/src/main.tsx` imports **after** `./index.css`, in this order:
  `styles/tokens.css` → `styles/base.css` → `styles/glass.css`.
  (After so they win on equal specificity; tokens first so the `--k-*` vars exist.)

## Please formalise during the split
1. Move the imports into the `index.css` aggregator as `@import` (top of file,
   before other rules) and remove the three lines from `main.tsx`.
2. `index.css` still contains the **now-duplicated** rules my partials override —
   they're harmless (mine win) but should be deleted in the split:
   - reset (`*`, `html,body,#root`, `body{…}`), scrollbar block → now in `base.css`
   - `.kinora-bg`, `.glass-card`, `.glass-input`, `.liquid-glass-dock`, `.footer-glass`,
     and the late "LIQUID GLASS" re-declaration block → now in `glass.css`
   - **Do NOT** move/alter button rules (`.glass-control`, `.login-btn`,
     `.nav-btn-*`, the blanket `button[...]` rules) — frozen by product mandate.
   - Leave motion/keyframes (Agent 4), login (Agent 11), book/wallbook, magic-layer
     where they are unless those owners move them.
3. The `:root` spring vars in `index.css` are Agent 4's (motion), not mine.

## Tooling (no action yet)
- No new deps required for the current type system (Fraunces/Newsreader via Google
  CDN, UI is system-first). If we bundle fonts later (Agent 7 perf), I'll request a
  `@fontsource/*` dep or vendored files under `src/assets/fonts/` via this file.
