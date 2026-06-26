// The cinematic login backdrop: the BookWall (shelves receding into the dark)
// lit like a screening room — a warm volumetric projector beam, drifting dust in
// the light, and a soft vignette that keeps the card legible. Every layer is
// transform/opacity only (GPU-cheap, 60fps) and collapses to a calm static scene
// under prefers-reduced-motion. A whisper of pointer parallax adds depth without
// ever fighting the form.
import { useEffect, useRef, type CSSProperties } from "react";
import BookWall from "../BookWall";
import type { BackdropVariant } from "./backdrop";

// Hand-scattered dust motes — left%, size px, rise duration s, negative delay so
// they're already mid-flight on first paint, horizontal drift px, opacity.
const MOTES = [
  { left: 12, size: 3, dur: 30, delay: -6, dx: 26, op: 0.5 },
  { left: 22, size: 2, dur: 38, delay: -19, dx: -22, op: 0.38 },
  { left: 30, size: 4, dur: 27, delay: -11, dx: 18, op: 0.46 },
  { left: 39, size: 2, dur: 42, delay: -3, dx: 30, op: 0.32 },
  { left: 46, size: 3, dur: 33, delay: -22, dx: -18, op: 0.52 },
  { left: 53, size: 5, dur: 25, delay: -8, dx: 22, op: 0.4 },
  { left: 60, size: 2, dur: 40, delay: -15, dx: -26, op: 0.34 },
  { left: 67, size: 3, dur: 29, delay: -2, dx: 20, op: 0.48 },
  { left: 74, size: 4, dur: 35, delay: -24, dx: -16, op: 0.42 },
  { left: 82, size: 2, dur: 44, delay: -10, dx: 24, op: 0.3 },
  { left: 88, size: 3, dur: 31, delay: -17, dx: -20, op: 0.46 },
] as const;

export default function AmbientBackdrop({
  variant,
  reducedMotion = false,
  rows = 5,
}: {
  variant: BackdropVariant;
  reducedMotion?: boolean;
  rows?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  // Whisper of pointer parallax (transform-only, rAF-throttled). Off under
  // reduced motion and on coarse pointers (touch). Writes --mx/--my in [-1,1].
  useEffect(() => {
    if (reducedMotion) return;
    if (typeof window !== "undefined" && window.matchMedia?.("(pointer: coarse)").matches) return;
    if (document.documentElement.classList.contains("kinora-balanced")) return;
    const el = ref.current;
    if (!el) return;
    let raf = 0;
    let tx = 0;
    let ty = 0;
    let lastX = 0;
    let lastY = 0;
    const onMove = (e: PointerEvent) => {
      tx = (e.clientX / window.innerWidth) * 2 - 1;
      ty = (e.clientY / window.innerHeight) * 2 - 1;
      if (Math.abs(tx - lastX) < 0.015 && Math.abs(ty - lastY) < 0.015) return;
      if (!raf) {
        raf = requestAnimationFrame(() => {
          raf = 0;
          lastX = tx;
          lastY = ty;
          el.style.setProperty("--mx", tx.toFixed(3));
          el.style.setProperty("--my", ty.toFixed(3));
        });
      }
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [reducedMotion]);

  const style = {
    "--beam-angle": `${variant.beamAngle}deg`,
    "--beam-x": `${variant.beamX}%`,
    "--warmth": variant.warmth,
  } as CSSProperties;

  return (
    <div
      ref={ref}
      className={`auth-backdrop${reducedMotion ? " is-static" : ""}`}
      style={style}
      aria-hidden="true"
    >
      <div className="auth-backdrop-parallax">
        <BookWall rows={rows} parallax={variant.parallax} />
      </div>

      {/* Push the wall back into shadow so it reads as a quiet, deep room and the
          beam/dust can catch the light on top of it. */}
      <div className="auth-walldim" />
      {/* Warm key light from above — the room's own glow. */}
      <div className="auth-keylight" />
      {/* The projector beam: a soft volumetric cone that slowly sways. */}
      <div className="auth-beam" />
      {/* Dust adrift in the light. */}
      <div className="auth-dust">
        {MOTES.map((m, i) => (
          <span
            key={i}
            className="auth-mote"
            style={
              {
                left: `${m.left}%`,
                width: m.size,
                height: m.size,
                "--mote-dur": `${m.dur}s`,
                "--mote-delay": `${m.delay}s`,
                "--mote-dx": `${m.dx}px`,
                "--mote-op": m.op,
              } as CSSProperties
            }
          />
        ))}
      </div>
      {/* Vignette + grain seal the room and protect the card's legibility. */}
      <div className="auth-vignette" />
      <div className="auth-grain" />
    </div>
  );
}
