import { forwardRef, type ReactNode, type ElementType } from "react";
import { motion } from "framer-motion";
import { useMotion } from "./MotionProvider";

/**
 * <Pressable> — tactile press/hover feedback for any control.
 *
 * Adds a spring squash on press and an optional lift on hover, WITHOUT
 * touching colour or shape (Agent 8 owns the button look — this is
 * transform-only). Honours reduced motion (becomes inert) and the global
 * speed knob via `useMotion`.
 *
 * For non-React / CSS-only surfaces, the `.mo-press` utility class in
 * motion.css does the same thing.
 */

export interface PressableProps {
  children: ReactNode;
  /** Element/component to render. Default `button`. */
  as?: ElementType;
  /** Scale at the bottom of the press. Default 0.96. */
  pressScale?: number;
  /** Scale on hover (1 = none). Default 1. */
  hoverScale?: number;
  className?: string;
  style?: React.CSSProperties;
  onClick?: (e: React.MouseEvent) => void;
  disabled?: boolean;
  type?: "button" | "submit" | "reset";
  "aria-label"?: string;
  [key: string]: unknown;
}

export const Pressable = forwardRef<HTMLElement, PressableProps>(function Pressable(
  { children, as = "button", pressScale = 0.96, hoverScale = 1, className, style, ...rest },
  ref,
) {
  const { spring, reduced } = useMotion();
  const Comp = motion(as as ElementType);

  return (
    <Comp
      ref={ref}
      className={className}
      style={style}
      whileTap={reduced ? undefined : { scale: pressScale }}
      whileHover={reduced || hoverScale === 1 ? undefined : { scale: hoverScale }}
      transition={spring("snappy")}
      {...rest}
    >
      {children}
    </Comp>
  );
});

export default Pressable;
