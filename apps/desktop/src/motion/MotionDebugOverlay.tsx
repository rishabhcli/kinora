import { useEffect, useRef, useState } from "react";
import { useMotion } from "./MotionProvider";

/**
 * <MotionDebugOverlay> — a developer HUD for the motion system.
 *
 * Toggle with ⌥⇧M (wired in MotionProvider). Shows live FPS, the active
 * global speed, and the reduced-motion state, plus a speed slider so you
 * can feel the whole app slow down / speed up. Rendered as a sibling of
 * the app; never ships in the user's way (hidden unless toggled).
 */
export function MotionDebugOverlay() {
  const { debug, speed, setSpeed, reduced } = useMotion();
  const [fps, setFps] = useState(60);
  const frames = useRef(0);
  const last = useRef(performance.now());

  useEffect(() => {
    if (!debug) return;
    let raf = 0;
    const loop = (now: number) => {
      frames.current++;
      if (now - last.current >= 500) {
        setFps(Math.round((frames.current * 1000) / (now - last.current)));
        frames.current = 0;
        last.current = now;
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [debug]);

  if (!debug) return null;

  const fpsColor = fps >= 55 ? "#34d399" : fps >= 40 ? "#fbbf24" : "#f87171";

  return (
    <div
      role="status"
      aria-live="off"
      className="fixed bottom-4 left-4 z-[2000] select-none rounded-xl px-3 py-2.5 font-mono text-[11px]"
      style={{
        background: "rgba(12, 11, 10, 0.92)",
        border: "1px solid rgba(255,255,255,0.1)",
        color: "rgba(232,226,216,0.92)",
        boxShadow: "0 12px 40px rgba(0,0,0,0.5)",
        minWidth: 168,
      }}
    >
      <div className="mb-1.5 flex items-center justify-between gap-4">
        <span className="opacity-60">motion · ⌥⇧M</span>
        <span style={{ color: fpsColor, fontWeight: 700 }}>{fps} fps</span>
      </div>
      <div className="mb-1 flex items-center justify-between">
        <span className="opacity-60">reduced</span>
        <span style={{ color: reduced ? "#fbbf24" : "rgba(232,226,216,0.55)" }}>
          {reduced ? "ON" : "off"}
        </span>
      </div>
      <div className="flex items-center justify-between">
        <span className="opacity-60">speed</span>
        <span>{speed.toFixed(2)}×</span>
      </div>
      <input
        type="range"
        min={0.25}
        max={4}
        step={0.05}
        value={speed}
        onChange={(e) => setSpeed(parseFloat(e.target.value))}
        aria-label="Global motion speed"
        className="mt-1.5 w-full"
        style={{ accentColor: "#d4a44e" }}
      />
    </div>
  );
}

export default MotionDebugOverlay;
