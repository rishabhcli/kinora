import { useEffect, useState } from "react";
import { AccessibilityInfo } from "react-native";

import { usePreferences } from "./usePreferences";

/**
 * Tracks the OS "reduce motion" accessibility setting, OR'd with the in-app
 * `reduceMotionOverride` preference so a user can force-quiet animations even
 * when the OS toggle is off.
 *
 * React Native has no built-in hook for the OS setting, so we read the initial
 * value with `AccessibilityInfo.isReduceMotionEnabled()` and subscribe to the
 * `reduceMotionChanged` event (the documented API, verified against the SDK 56
 * / RN 0.85 docs). Callers use it to drop animated entrances and transitions.
 */
export function useReducedMotion(): boolean {
  const [osReduced, setOsReduced] = useState(false);
  const override = usePreferences((state) => state.reduceMotionOverride);

  useEffect(() => {
    let mounted = true;
    void AccessibilityInfo.isReduceMotionEnabled().then((value) => {
      if (mounted) setOsReduced(value);
    });
    const sub = AccessibilityInfo.addEventListener("reduceMotionChanged", (value) => {
      setOsReduced(value);
    });
    return () => {
      mounted = false;
      sub.remove();
    };
  }, []);

  return osReduced || override;
}
