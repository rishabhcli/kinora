import { Children, isValidElement, useRef, type ReactNode } from "react";
import { motion, useInView } from "framer-motion";
import { useMotion } from "./MotionProvider";
import {
  fadeIn,
  staggerContainer,
  staggerItem,
  type RevealDirection,
} from "./variants";

type HtmlTag = "div" | "section" | "ul" | "ol" | "li" | "span" | "header" | "nav" | "article";

export interface RevealProps {
  children: ReactNode;
  /**
   * Stagger the direct children. `true` uses the default cadence; a
   * number sets the per-child delay (seconds). When set, <Reveal> becomes
   * the orchestrating container and wraps each child in a stagger item.
   */
  stagger?: boolean | number;
  /** Offset the whole group's entrance (seconds). */
  delay?: number;
  /** The element tag to render. Default `div`. */
  as?: HtmlTag;
  /** The element tag for each staggered child wrapper. Default `div`. */
  itemAs?: HtmlTag;
  /** Slide direction for the entrance. Default `up`. */
  direction?: RevealDirection;
  /** Slide distance in px. Default 14 (single) / 16 (stagger item). */
  distance?: number;
  /** Re-trigger every time it scrolls into view (default: once). */
  repeat?: boolean;
  /** Fraction of the element visible before triggering (0–1 or keyword). */
  amount?: number | "some" | "all";
  className?: string;
  itemClassName?: string;
  style?: React.CSSProperties;
}

const DEFAULT_STAGGER = 0.06;

/**
 * <Reveal> — entrance choreography on scroll-into-view.
 *
 * Two modes:
 *   • single  — the element fades + slides in as one block.
 *   • stagger — children cascade in (pass `stagger`).
 *
 * Always honours reduced motion (collapses to an instant opacity reveal)
 * and the global speed knob via `useMotion`. Safe without a provider.
 */
export function Reveal({
  children,
  stagger,
  delay = 0,
  as = "div",
  itemAs = "div",
  direction = "up",
  distance,
  repeat = false,
  amount = 0.2,
  className,
  itemClassName,
  style,
}: RevealProps) {
  const ref = useRef<HTMLElement>(null);
  const inView = useInView(ref, { once: !repeat, amount });
  const { spring, reduced } = useMotion();
  const transition = spring("gentle");

  const Container = motion[as] as typeof motion.div;

  if (stagger) {
    const Item = motion[itemAs] as typeof motion.div;
    const perChild = typeof stagger === "number" ? stagger : DEFAULT_STAGGER;
    const container = staggerContainer(reduced ? 0 : perChild, reduced ? 0 : delay);
    const item = staggerItem(transition, direction, distance ?? 16);

    return (
      <Container
        ref={ref as never}
        className={className}
        style={style}
        variants={container}
        initial="hidden"
        animate={inView ? "show" : "hidden"}
      >
        {Children.map(children, (child, i) =>
          isValidElement(child) ? (
            <Item key={child.key ?? i} className={itemClassName} variants={item}>
              {child}
            </Item>
          ) : (
            child
          ),
        )}
      </Container>
    );
  }

  const single = fadeIn(transition, direction, distance ?? 14);
  return (
    <Container
      ref={ref as never}
      className={className}
      style={style}
      variants={single}
      initial="hidden"
      animate={inView ? "show" : "hidden"}
      transition={delay && !reduced ? { ...transition, delay } : undefined}
    >
      {children}
    </Container>
  );
}

export default Reveal;
