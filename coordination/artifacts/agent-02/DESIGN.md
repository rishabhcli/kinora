# Agent 02 — Scroll Film Engine: design

> The single most important client illusion in Kinora: **scrolling a book feels
> like scrubbing one continuous film.** This replaces ReadingRoom's per-shot
> `<video>` swap + 0.55s crossfade with continuous-timeline scrubbing.

## The core mapping

```
scroll fraction (0..1)
   → focusWord            focusWordFromFraction(frac, totalWords)   [mirrors ReadingRoom]
   → { segment, localFraction }   resolvePlayhead(timeline, focusWord)
   → currentTime (s)      segmentTime(segment, localFraction, liveDuration?)
   → <video>.currentTime  (set imperatively in a rAF loop — never React state per frame)
```

All of this math lives in `timeline.ts` as pure functions (unit-tested). The hook
and components are the only parts that touch the DOM.

## The timeline (sync map)

A `Timeline` is an ordered, **contiguous** list of `FilmSegment`s. Each maps a
global word range `[wordStart, wordEnd)` onto a `[clipStart, clipEnd]` window
inside its source mp4. Three shapes feed `buildTimeline()` uniformly:

| Source | segments | crossfade points |
|---|---|---|
| **Per-shot clips** (today's backend: one mp4 per shot) | one per shot, `clipStart=0`, `clipEnd=duration_s`, distinct `src` | every boundary (src changes) |
| **Stitched event films** (Agent 1) | several shots share one event `src` with rising `clipStart` | only at event boundaries |
| **Single bundled film** (no backend / `live=false`) | one segment spanning all words, `clipEnd=0` ⇒ use live `<video>.duration` | none |

Because consecutive segments that share a `src` are scrubbed by `currentTime`
alone, the FilmPane only crossfades when `src` actually changes — which is
**event-level** when stitched films exist (WS2) and shot-level otherwise, with no
special-casing. Gaps between word ranges are absorbed into the earlier segment so
scrubbing never lands in a dead zone (never a frozen/empty pane — mission rule).

## Scrub vs play (velocity-aware)

- **Fast flick / active drag** → `scrub`: pin `currentTime` to the scroll-derived
  target every rAF; the video is effectively a scrubber.
- **Slow reading / at rest** → `play`: release; let the segment's clip play
  forward (looping) as ambient scene motion — the "generates a few seconds ahead"
  feel. We stop seeking and only re-engage scrub when velocity crosses threshold
  again (`classifyScroll`, default 16 wps) **or** a fresh scroll arrives, then
  settle back to play after a short at-rest debounce.

This preserves ReadingRoom's at-rest behaviour (the looping per-shot clip) while
adding true scrubbing under motion.

## Performance (60fps, GPU-only)

- Scroll is read from a ref in a single `requestAnimationFrame` loop. `currentTime`,
  parallax `translate3d`, and the scrub indicator are written imperatively to refs
  — **no React `setState` in the hot path.** Low-frequency state (active paragraph,
  progress rail, focus word for the scheduler) is throttled (~120ms).
- Only `transform`/`opacity` animate. Crossfade is opacity-only, ≤2 `<video>`
  layers (WS2). `will-change: transform` on the parallax layers.

## Reduced motion

`reducedMotion` (prop; defaults to framer-motion `useReducedMotion()`) degrades to
**instant cuts**: crossfade duration 0 (promote the incoming layer immediately),
`currentTime` snapped directly to target (no smoothing), no parallax, no inertia.

## Scheduler signalling (preserved verbatim)

`schedulerSignal(prevWord, word, dt)` reproduces ReadingRoom 197–204: a jump
>120 words → `api.seek`; otherwise `api.postIntent(word, velocity)` with velocity
clamped to `[2,12]` (default 4 when `dt<=0`). Fired only when `sessionId` is set.

## Decoupling / seams (why this builds in isolation)

`timeline.ts` is pure and imports nothing from `lib/api.ts` (Agent 12) or the
not-yet-existing event-film API (Agent 1/3) or `src/motion/` (Agent 6). The engine
takes everything via props with safe fallbacks, so `typecheck && build` is green on
the `main` baseline today and stays green as siblings land. See
`coordination/requests/agent-04.md` for the consumed seams.
