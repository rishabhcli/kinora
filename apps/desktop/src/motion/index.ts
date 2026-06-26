/**
 * @kinora motion system — the app-wide motion vocabulary (Agent 04).
 *
 * Import primitives from here, not from individual files. Importing this
 * barrel also loads `motion.css` (the tokens + consolidated keyframes), so
 * any consumer of a primitive automatically gets the motion stylesheet.
 *
 * Public API (kept stable; documented in coordination/CONTRACTS.md):
 *   <MotionProvider>      reduced-motion + global speed + debug context
 *   useMotion()           the ergonomic hook (spring/tween/reduced/speed)
 *   <Reveal>              in-view entrance, optional stagger
 *   <PageTransition>      route/page cross-transition
 *   <BookOpenTransition>  shared-element book → film morph
 *   <ShelfScroller>       inertial, snap-aware horizontal rail w/ parallax
 *   <Tilt> / useTilt()    3D cover hover with reduced-transparency glare
 *   <Pressable>           tactile press/hover feedback (transform-only)
 *   <MotionDebugOverlay>  FPS + active-spring HUD (⌥⇧M)
 *   springs / ease / variants / shared-element helpers
 */
import "../styles/motion.css";

export { MotionProvider, useMotion } from "./MotionProvider";
export { useReducedMotionPref } from "./useReducedMotionPref";
export { Reveal } from "./Reveal";
export type { RevealProps } from "./Reveal";
export { PageTransition } from "./PageTransition";
export { BookOpenTransition } from "./BookOpenTransition";
export type { BookOpenTransitionProps, CoverArt } from "./BookOpenTransition";
export { ShelfScroller } from "./ShelfScroller";
export type { ShelfScrollerProps } from "./ShelfScroller";
export { Tilt, useTilt } from "./Tilt";
export type { TiltProps, TiltOptions } from "./Tilt";
export { Pressable } from "./Pressable";
export type { PressableProps } from "./Pressable";
export { MotionDebugOverlay } from "./MotionDebugOverlay";

export {
  SPRINGS,
  EASE,
  DURATION,
  spring,
  tween,
  scaleTransition,
} from "./springs";
export type {
  SpringPreset,
  EaseToken,
  DurationToken,
  SpringTransition,
  TweenTransition,
  MotionTransition,
} from "./springs";

export {
  fadeIn,
  scaleIn,
  staggerContainer,
  staggerItem,
  pageVariants,
} from "./variants";
export type { RevealDirection } from "./variants";

export {
  getRect,
  flipFrom,
  heroCoverRect,
  coverRectFromEvent,
  useSharedElement,
} from "./useSharedElement";
export type { Rect } from "./useSharedElement";
