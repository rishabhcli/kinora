# Kinora overnight build — shared CONTRACTS

Each agent appends its published API here and keeps it **stable**. Consume via
the documented surface; stub against it if a producer hasn't merged yet (Agent
12 swaps real imports at integration).

---

## Agent 04 — Motion system (`apps/desktop/src/motion`)

The app-wide motion vocabulary. Import from the barrel `@/motion`
(`apps/desktop/src/motion`); importing it also loads `src/styles/motion.css`
(motion tokens + consolidated keyframes). Every primitive is **reduced-motion
aware** (collapses to instant/opacity-only) and honours the global speed knob.
GPU-only (transform/opacity); never animates layout.

### Provider + hook

```tsx
<MotionProvider initialSpeed?={number}>…</MotionProvider>
```
Root of the system: exposes reduced-motion + a global SPEED knob (mirrored to
the `--mo-speed` CSS var so CSS transitions scale too) + a debug toggle (⌥⇧M).
Wraps children in `<MotionConfig reducedMotion="user">`. Mount as high as
possible. (Currently mounted in `HomePage`; see request for login coverage.)

```ts
const { reduced, speed, setSpeed, debug, setDebug, spring, tween } = useMotion();
spring(preset?: "gentle" | "snappy" | "cinematic"): Transition   // speed/reduced-aware
tween(duration?: DurationToken, ease?: EaseToken): Transition
```
`useMotion()` works **without** a provider (falls back to OS reduced-motion,
speed 1) so primitives are safe to adopt before the provider is mounted
app-wide.

### Primitives

```tsx
<Reveal stagger? delay? as? itemAs? direction? distance? repeat? amount?>
```
In-view entrance. `stagger` (bool | number) cascades direct children. Replaces
ad-hoc shelf/list stagger.

```tsx
<PageTransition activeKey={string}>{node}</PageTransition>
```
Route/page cross-dissolve + settle. Replaces `AnimatedPageSwitch` (now a
deprecated shim → this).

```tsx
<BookOpenTransition open originRect cover onOpened? onClosed?>
  {(opened: boolean) => <ReadingRoom book={opened ? book : null} … />}
</BookOpenTransition>
```
Shared-element book→film morph. **Division of labour:** this owns the cover
TRAVEL (shelf rect → centred hero box, transform-only FLIP) + the focus scrim
+ a static "settle" hold that hides the room's mount cost; the **reading room
(Agent 10) owns the REVEAL** (its cover hinge). Render-prop gates the room's
mount until travel lands. `cover: { image?, gradient? }`. Reduced motion → the
room mounts immediately (clean fade). Consumed by `HomePage` (Agents 10/3/9
hook into the room it wraps).

```tsx
<ShelfScroller snap? wheelHorizontal? arrows? backdrop? gap? railClassName?>{covers}</ShelfScroller>
```
Inertial horizontal rail: drag-fling momentum, vertical-wheel→horizontal (with
edge-release to the page), velocity-projected snap, rubber-band, parallax
backdrop, edge depth-of-field mask, content-visibility for 100+ covers. **Agent
5 wraps real book rows.** Drag suppresses the trailing click so a fling never
opens a book. Origin-rect capture for the morph keys off the `.book-cover`
class or a `[data-shared-cover]` attr.

```tsx
<Tilt rotateDepth? translateDepth? perspective? glare?>{cover}</Tilt>
const { ref, innerRef, glareRef, bind, reduced } = useTilt(opts)
```
3D pointer tilt (generalises `CometCard`). Direct DOM writes (no per-frame
re-render). Glare hidden under `prefers-reduced-transparency`. **Agent 5** can
replace `CometCard` with this.

```tsx
<Pressable as? pressScale? hoverScale? …/>     // transform-only press feedback
<MotionDebugOverlay/>                          // FPS + speed HUD (⌥⇧M)
```

### Tokens / helpers (also exported from the barrel)

- `SPRINGS`, `EASE`, `DURATION`, `spring()`, `tween()`, `scaleTransition()`
- `fadeIn`, `scaleIn`, `staggerContainer`, `staggerItem`, `pageVariants`
- `getRect`, `flipFrom`, `heroCoverRect`, `coverRectFromEvent`, `useSharedElement`
- CSS twins in `motion.css`: `--mo-ease-*`, `--mo-dur-*`, `--mo-t-*` (speed-scaled),
  utility classes `.mo-press`, `.mo-press-soft`, `.mo-focus-ring`, `.mo-hover`,
  `.mo-edge-fade-x`, `.mo-tilt-glare`, `.mo-shelf-rail`.

### Consumes (seams — stubbed until integration)

- **Agent 6** `useReducedMotionPref()` from `src/a11y/` — currently stubbed in
  `src/motion/useReducedMotionPref.ts` (wraps framer-motion's `useReducedMotion`).
- **Agent 8** design tokens — motion.css holds *only* timing/easing/transform;
  colours/shadows come from Agent 8's tokens.
