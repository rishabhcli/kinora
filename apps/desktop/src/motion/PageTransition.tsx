import type { ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useMotion } from "./MotionProvider";
import { pageVariants } from "./variants";

/**
 * <PageTransition> — the route/page cross-transition.
 *
 * Replaces the ad-hoc `AnimatedPageSwitch`: a refined cross-dissolve +
 * settle where the outgoing page sinks and dims while the incoming page
 * rises into focus. Transform/opacity only, so it stays buttery on every
 * tab. Reduced motion collapses to an opacity-only swap (a transform here
 * would re-anchor the app's fixed navbar — opacity is deliberate).
 *
 * Drive it from a single key that changes per page:
 *   <PageTransition activeKey={activePage}>{pages[activePage]}</PageTransition>
 */
export function PageTransition({
  activeKey,
  children,
}: {
  activeKey: string;
  children: ReactNode;
}) {
  const { spring, reduced } = useMotion();
  const variants = pageVariants(spring("gentle"), reduced);

  return (
    <AnimatePresence mode="wait" initial={false}>
      <motion.div
        key={activeKey}
        variants={variants}
        initial="hidden"
        animate="show"
        exit="exit"
        style={{
          position: "relative",
          zIndex: 1,
          transformOrigin: "top center",
        }}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}

export default PageTransition;
