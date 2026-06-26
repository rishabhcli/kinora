# Agent 11 — Login Experience: Design Notes

Persistent design plan + "what I've tried" log so each Ralph iteration builds on the last.

## Thesis (the subject's world)
Kinora = **a private screening room for literature**. The login is the hush *before the film
starts*: a darkened theatre, a warm projector beam, and a wall of books that are "what's showing."
Signing in = **opening the cover → the film begins.**

This is NOT one of the generic AI looks (cream/serif/terracotta, near-black + acid accent, or
broadsheet). It's a **warm theatre dark + brass/gilt + cream-page** identity, inherited from the
existing Kinora palette and deepened.

## Tokens (formalized in styles/login.css `.kinora-auth` scope; Agent 8 can repoint)
Color (one source of truth — no hardcoded hex in markup):
- `--auth-abyss #0a0908` · `--auth-bg #0e0d0c` · `--auth-bg-2 #141210` · `--auth-surface #181614`
- `--auth-ink #f4efe6` (headings) · `--auth-text #e8e2d8` · `--auth-muted #b3a99d` (AA-bumped)
- `--auth-subtle #6b6258` · `--auth-gold #d4a44e` · `--auth-gold-bright #e8c878`
- `--auth-line rgba(232,226,216,.10)` (hairlines derive from ink, not pure white)
- `--auth-beam rgba(212,164,78,.16)` (projector warmth)

Type: **Fraunces** (display serif — literary, optical, high-contrast) for wordmark/tagline/card
title; **DM Sans** for UI/body/inputs. Already in the stack; tighten scale + tracking + weights.

## Layout
Full-bleed cinematic backdrop → left brand rail (logo lockup, big serif tagline, microlabel) →
centered/offset frosted auth card. Responsive: brand collapses above the card on narrow widths.

## Signature (spend boldness here, keep everything else quiet)
**Volumetric projector beam + drifting dust in a screening room**, with a cold-launch "projector
warms up" intro and a sign-in "the library opens" threshold. One bold idea; the form stays
disciplined.

## Motion
- Backdrop: parallax shelves (depth via per-row scale/opacity/speed + faint blur on far rows),
  slow beam sway, dust drift, subtle bloom. transform/opacity only. 60fps. (WS1)
- Cold launch: ~1.2s warm-up — vignette opens, beam fades in, wordmark+tagline rise, card last.
  Fires once per launch (sessionStorage guard). (WS4)
- Form: focus micro-interactions; Sign In↔Sign Up morph (framer-motion height + cross-fade);
  submit idle→loading→success→error(shake). (WS2)
- Enter: login card recedes (scale↑ + fade) while wall blooms; home cross-fades in **opacity-only**
  to protect the fixed navbar (transform/filter/backdrop-filter on the home wrapper would become the
  containing block for `position:fixed` and break the navbar anchor). (WS3)
- Reduced motion: static lit backdrop, instant morph, plain cross-fade enter. (all WS)

## A11y (Agent 6 contract — stubbed locally, real import at integration)
Visible labels (not placeholder-only), `aria-live` error region, `:focus-visible` rings, logical tab
order, password show/hide as a real toggle (`aria-pressed`), real checkbox+label for remember-me,
labeled social buttons, `autocomplete` attrs, AA contrast over the backdrop, reduced-motion honored.

## Pure functions (TDD — node --test, Node 26 strips .ts; zero deps, see tests/auth/)
1. `validateEmail(value) -> string | null`
2. `validatePassword(value, mode) -> string | null`
3. `passwordStrength(value) -> 0..4` + label
4. `coverPrefetchList(books, limit) -> string[]` (dedupe + cap)
5. `pickBackdropVariant(seed)` deterministic per-launch variant

## Cover cache (system design)
`warmCoverCache(urls)` prefetches via `new Image()` (warms browser cache) — fire-and-forget on
successful auth, offline-safe (never throws). Source = Agent 5 cover API (stubbed to local
`data/books` cover URLs today; Agent 12 swaps the source at integration).

## Tried / decisions log
- ⚠️ LOOP-STATE COLLISION: my session is rooted in the MAIN repo (`/Users/.../kinora`),
  and sibling agents (a02/a04/a09) clobber the shared `.claude/ralph-loop.local.md`, so the Ralph
  stop hook sometimes re-feeds the WRONG agent's mission (saw Agent 02). My true task is fixed:
  **Agent 11 / promise `AGENT 11 COMPLETE`** (per the original user command). On any wrong re-feed:
  `cd /Users/m3-max/Documents/GitHub/kinora && bash agent-prompts/arm-ralph.sh 11`, then continue
  Agent 11. NEVER output another agent's promise.
- (iter 1) Established thesis + tokens + plan. BookWall already glides on glass — ELEVATE (depth,
  beam, dust), don't replace. Bundled `public/generated/film-0X.mp4` are 720x1280 — kept as an
  OPTIONAL ambient layer but default OFF (a blurred film fighting card legibility is a risk; the
  shelves are the stronger, on-brand signature). Revisit only if depth alone feels flat.
