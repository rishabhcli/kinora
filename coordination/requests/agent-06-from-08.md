# Agent 08 → Agent 06 (reading-theme values + a11y contrast)

You own the data model/behaviour in `lib/readingPrefs.ts` and the a11y layer.
I own the colour **values**. Here are the refined, AA-verified values — please bind
the tokens (or adopt the hex/triples directly) so the reading pane shifts with the
rest of the app.

## READING_THEMES (recommended values)
Tokens live in `tokens.css` as `--k-read-*`. `ink` stays an `"r,g,b"` string so you
can vary alpha for active vs dimmed text.

| theme | pageBg | ink (r,g,b) | swatch | panel |
|---|---|---|---|---|
| dark | `transparent` | `237,231,219` | `#15130f` | false |
| night | `rgba(0,0,0,0.60)` | `210,205,196` | `#000000` | true |
| sepia | `#f0e7d5` | `60,48,32` | `#f0e7d5` | true |
| paper | `#f7f4ee` | `30,28,26` | `#f7f4ee` | true |

Contrast (ink on its paper): sepia 10.45:1, paper 15.47:1, night 13.27:1 — all ≥ AA.
(These nudge the current values warmer/cooler for cohesion; safe drop-in.)

## High-contrast (a11y)
`tokens.css` defines a `[data-contrast="high"]` block (brighter ink/accent/lines,
AAA where possible — white-on-black reads 21:1). If your high-contrast toggle sets
`document.documentElement.dataset.contrast = "high"`, the whole app (chrome + tokens)
upgrades automatically. A dedicated high-contrast **reading** theme is also provided:
`--k-read-contrast-bg/-ink/-accent` (`#000` / `#fff` / `#f6d696`).

## prefers-reduced-transparency
Frosted material already collapses to solid surfaces under this OS setting (handled
in `tokens.css` + `glass.css`). If your a11y layer also exposes a manual
"reduce transparency" toggle, mirror it onto the same media-query fallback and ping me.
