import type { Variants } from "framer-motion";
import type { MotionTransition } from "./springs";

/**
 * variants.ts — the app's reusable entrance/exit choreography.
 *
 * These are framer-motion `Variants` builders. They take a resolved
 * transition (already speed/reduced-aware, from `useMotion`) so the same
 * shape can be calm, fast, or instant depending on context. Keep them
 * transform/opacity only — never animate layout.
 *
 * Naming: `hidden` → `show` (entrance), with an optional `exit`.
 */

export type RevealDirection = "up" | "down" | "left" | "right" | "none";

const offsetFor = (dir: RevealDirection, distance: number) => {
  switch (dir) {
    case "up":
      return { y: distance };
    case "down":
      return { y: -distance };
    case "left":
      return { x: distance };
    case "right":
      return { x: -distance };
    case "none":
      return {};
  }
};

/** A single element reveal: fade + a small directional slide. */
export function fadeIn(
  transition: MotionTransition,
  dir: RevealDirection = "up",
  distance = 14,
): Variants {
  const offset = offsetFor(dir, distance);
  return {
    hidden: { opacity: 0, ...offset },
    show: { opacity: 1, x: 0, y: 0, transition },
    exit: { opacity: 0, ...offset, transition: { ...transition, duration: 0.18 } },
  };
}

/** A reveal with a gentle scale — for cards / hero media. */
export function scaleIn(transition: MotionTransition, from = 0.94): Variants {
  return {
    hidden: { opacity: 0, scale: from },
    show: { opacity: 1, scale: 1, transition },
    exit: { opacity: 0, scale: from, transition: { ...transition, duration: 0.18 } },
  };
}

/**
 * A stagger CONTAINER: orchestrates its children's entrances. Pair with
 * `staggerItem`. `stagger` is the per-child delay; `delay` offsets the
 * whole group.
 */
export function staggerContainer(stagger = 0.06, delay = 0): Variants {
  return {
    hidden: {},
    show: {
      transition: {
        staggerChildren: stagger,
        delayChildren: delay,
      },
    },
    exit: {
      transition: { staggerChildren: stagger / 2, staggerDirection: -1 },
    },
  };
}

/** A stagger CHILD — inherits timing from its `staggerContainer` parent. */
export function staggerItem(
  transition: MotionTransition,
  dir: RevealDirection = "up",
  distance = 16,
): Variants {
  const offset = offsetFor(dir, distance);
  return {
    hidden: { opacity: 0, ...offset },
    show: { opacity: 1, x: 0, y: 0, transition },
    exit: { opacity: 0, ...offset },
  };
}

/**
 * The page cross-transition variants — outgoing sinks + dims, incoming
 * rises into focus. Used by <PageTransition>. `reduced` collapses it to
 * an opacity-only swap (no transform that could re-anchor fixed chrome).
 */
export function pageVariants(transition: MotionTransition, reduced: boolean): Variants {
  if (reduced) {
    return {
      hidden: { opacity: 0 },
      show: { opacity: 1, transition },
      exit: { opacity: 0, transition },
    };
  }
  return {
    hidden: { opacity: 0, y: 16, scale: 0.992 },
    show: { opacity: 1, y: 0, scale: 1, transition },
    exit: { opacity: 0, y: -10, scale: 0.996, transition: { ...transition, duration: 0.22 } },
  };
}
