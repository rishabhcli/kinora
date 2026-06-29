// Allow the Electron `-webkit-app-region` drag hint in inline styles. It powers
// the custom hidden title bar: the navbar is a drag region and its interactive
// clusters opt out with `no-drag`. React's CSSProperties (an interface extending
// csstype) omits this non-standard property, so we add it here. framer-motion's
// MotionStyle builds on CSSProperties, so this flows through to motion.* too.
import "react";

declare module "react" {
  interface CSSProperties {
    WebkitAppRegion?: "drag" | "no-drag";
  }
}
