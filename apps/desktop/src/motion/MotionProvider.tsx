import {
  createContext,
  useContext,
  useMemo,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import { MotionConfig } from "framer-motion";
import {
  spring as resolveSpring,
  tween as resolveTween,
  type SpringPreset,
  type DurationToken,
  type EaseToken,
  type MotionTransition,
  type TweenTransition,
} from "./springs";
import { useReducedMotionPref } from "./useReducedMotionPref";

/**
 * MotionProvider — the root of the motion system.
 *
 * Responsibilities:
 *   1. Expose the user's reduced-motion intent (via the a11y seam).
 *   2. Own the global SPEED knob (1 = normal). Both the framer-motion
 *      layer (numeric) and the CSS layer (the `--mo-speed` var) read it,
 *      so one slider rescales the entire app's motion.
 *   3. Own a DEBUG toggle for the FPS / active-spring overlay.
 *   4. Default every descendant framer-motion transition (via
 *      <MotionConfig>) and honour the OS reduced-motion setting.
 *
 * Mount it as high as possible. Agent 04 mounts it in HomePage (the app
 * shell it owns); a request is filed for Agent 11/12 to also wrap the
 * login screen so the very first frame is governed too.
 */

interface MotionContextValue {
  /** True when motion should be removed (OS preference or forced). */
  reduced: boolean;
  /** Global speed multiplier; larger = faster. */
  speed: number;
  setSpeed: (s: number) => void;
  /** Debug overlay visibility (FPS + active springs). */
  debug: boolean;
  setDebug: (d: boolean) => void;
  /** Resolve a spring preset into a speed/reduced-aware transition. */
  spring: (preset?: SpringPreset) => MotionTransition;
  /** Resolve a tween from the duration + ease scales. */
  tween: (duration?: DurationToken, ease?: EaseToken) => TweenTransition;
}

const MotionContext = createContext<MotionContextValue | null>(null);

const MIN_SPEED = 0.25;
const MAX_SPEED = 4;
const clampSpeed = (s: number) => Math.min(MAX_SPEED, Math.max(MIN_SPEED, s));

export function MotionProvider({
  children,
  initialSpeed = 1,
}: {
  children: ReactNode;
  initialSpeed?: number;
}) {
  const reduced = useReducedMotionPref();
  const [speed, setSpeedState] = useState(() => clampSpeed(initialSpeed));
  const [debug, setDebug] = useState(false);

  const setSpeed = useCallback((s: number) => setSpeedState(clampSpeed(s)), []);

  // Mirror the speed knob into CSS so `calc(time * var(--mo-speed))`
  // transitions track it. Reduced motion is handled by the CSS media
  // query, so we only push speed here.
  useEffect(() => {
    const root = document.documentElement;
    root.style.setProperty("--mo-speed", String(1 / speed));
    return () => {
      root.style.removeProperty("--mo-speed");
    };
  }, [speed]);

  // A discreet developer affordance: ⌥⇧M toggles the motion debug overlay.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey && e.shiftKey && (e.key === "m" || e.key === "M")) {
        e.preventDefault();
        setDebug((d) => !d);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const value = useMemo<MotionContextValue>(
    () => ({
      reduced,
      speed,
      setSpeed,
      debug,
      setDebug,
      spring: (preset: SpringPreset = "gentle") =>
        resolveSpring(preset, speed, reduced),
      tween: (duration: DurationToken = "base", ease: EaseToken = "standard") =>
        resolveTween(duration, ease, speed, reduced),
    }),
    [reduced, speed, setSpeed, debug],
  );

  return (
    <MotionContext.Provider value={value}>
      <MotionConfig reducedMotion="user">{children}</MotionConfig>
    </MotionContext.Provider>
  );
}

/**
 * useMotion — the ergonomic hook every primitive and call site uses.
 *
 * Works WITHOUT a provider (falls back to OS reduced-motion + speed 1),
 * so primitives are safe to drop anywhere and other agents can adopt
 * them before <MotionProvider> is mounted app-wide.
 */
export function useMotion(): MotionContextValue {
  const ctx = useContext(MotionContext);
  // Hook order is stable: this hook always runs; the value is only used
  // for the fallback path.
  const fallbackReduced = useReducedMotionPref();
  return useMemo<MotionContextValue>(() => {
    if (ctx) return ctx;
    return {
      reduced: fallbackReduced,
      speed: 1,
      setSpeed: () => {},
      debug: false,
      setDebug: () => {},
      spring: (preset: SpringPreset = "gentle") =>
        resolveSpring(preset, 1, fallbackReduced),
      tween: (duration: DurationToken = "base", ease: EaseToken = "standard") =>
        resolveTween(duration, ease, 1, fallbackReduced),
    };
  }, [ctx, fallbackReduced]);
}
