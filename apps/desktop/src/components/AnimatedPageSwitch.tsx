import { ReactNode } from "react";
import { PageTransition } from "../motion";

/**
 * @deprecated Use `<PageTransition>` from `src/motion` directly.
 *
 * Kept as a thin shim so any caller that still imports AnimatedPageSwitch
 * keeps working after the motion-system migration. It now delegates to the
 * shared <PageTransition> primitive (which is reduced-motion + speed aware)
 * instead of carrying its own ad-hoc easing.
 */
export default function AnimatedPageSwitch({
  active,
  pages,
}: {
  active: string;
  pages: Record<string, ReactNode>;
}) {
  return <PageTransition activeKey={active}>{pages[active]}</PageTransition>;
}
