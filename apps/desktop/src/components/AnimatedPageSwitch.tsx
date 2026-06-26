import { ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

// A soft cinematic settle — the outgoing page sinks and dims, the incoming page
// rises into focus. Transform/opacity only, so it stays buttery on every tab.
const EASE: [number, number, number, number] = [0.22, 1, 0.36, 1];

export default function AnimatedPageSwitch({
  active,
  pages,
}: {
  active: string;
  pages: Record<string, ReactNode>;
}) {
  const reduce = useReducedMotion();

  return (
    <AnimatePresence mode="wait" initial={false}>
      <motion.div
        key={active}
        initial={reduce ? { opacity: 0 } : { opacity: 0, y: 16, scale: 0.99 }}
        animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
        exit={reduce ? { opacity: 0 } : { opacity: 0, y: -10, scale: 0.995 }}
        transition={{ duration: reduce ? 0.2 : 0.36, ease: EASE }}
        style={{ position: "relative", zIndex: 1, transformOrigin: "top center", willChange: "transform, opacity" }}
      >
        {pages[active]}
      </motion.div>
    </AnimatePresence>
  );
}
