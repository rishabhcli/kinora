import { useEffect, useState } from "react";
import { AccessibilityInfo } from "react-native";

/**
 * Tracks the OS "reduce motion" accessibility setting.
 *
 * React Native has no built-in hook for this, so we read the initial value with
 * `AccessibilityInfo.isReduceMotionEnabled()` and subscribe to the
 * `reduceMotionChanged` event (the documented API, verified against the SDK 56
 * / RN 0.85 docs). Callers use it to drop animated entrances and transitions.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    let mounted = true;
    void AccessibilityInfo.isReduceMotionEnabled().then((value) => {
      if (mounted) setReduced(value);
    });
    const sub = AccessibilityInfo.addEventListener("reduceMotionChanged", (value) => {
      setReduced(value);
    });
    return () => {
      mounted = false;
      sub.remove();
    };
  }, []);

  return reduced;
}
