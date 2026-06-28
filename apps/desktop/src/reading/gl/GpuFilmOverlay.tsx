// The <canvas> overlay that runs the WebGL compositor over the FilmPane's CSS
// crossfade. It is a pure enhancement: it samples the two visible <video> layers
// as textures and adds GPU crossfade + colour grade + grain. It mounts the canvas
// only when capabilities.decideCompositor() approves, and it HIDES the canvas
// (opacity 0) the instant the compositor stops being operational (context lost,
// program failed, or layer A has no decoded frame) — revealing the proven CSS
// film underneath. So the overlay can never be the source of a black frame.
//
// Adoption is non-invasive: the host passes getters that return the current
// outgoing / incoming <video> elements and the crossfade mix. FilmPane can expose
// those without changing its no-black-frame logic. Until it does, this component
// is fully self-contained and unit-driven through its props.
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { WebGLCompositor, type FrameSource } from "./webglCompositor";
import { decideCompositor, probeGl, type GlCapabilities } from "./capabilities";
import { VERT_SRC, FRAG_SRC } from "./shaders";
import { NEUTRAL_GRADE, type FilmGrade } from "./grade";

export interface GpuFilmOverlayProps {
  /** returns the current [outgoing, incoming] <video> elements (incoming may be null) */
  getLayers: () => [HTMLVideoElement | null, HTMLVideoElement | null];
  /** returns the crossfade position 0..1 (0 = outgoing fully shown) */
  getMix: () => number;
  /** the colour grade to apply (default neutral = passthrough) */
  grade?: FilmGrade;
  /** app-wide reduced-motion preference — disables the GPU pass entirely */
  reducedMotion: boolean;
  /** a rolling jank ratio from usePerfMonitor (high → don't run the GPU pass) */
  jankRatio?: number;
  /** explicit opt-out */
  forceOff?: boolean;
  className?: string;
  style?: CSSProperties;
  /** test seam: inject capabilities instead of probing a real GPU */
  capsOverride?: GlCapabilities;
}

/** Wrap a <video> as a compositor FrameSource (decoded once readyState ≥ 2). */
function videoSource(video: HTMLVideoElement): FrameSource {
  return {
    element: video,
    hasFrame: () => video.readyState >= 2 && video.videoWidth > 0,
    flipY: false, // HTMLVideoElement uploads top-down; we flip in-shader if needed
  };
}

// Probe once per process — the GPU's capabilities don't change at runtime.
let cachedCaps: GlCapabilities | null = null;
function getCaps(override?: GlCapabilities): GlCapabilities {
  if (override) return override;
  if (!cachedCaps) cachedCaps = probeGl({ vertSrc: VERT_SRC, fragSrc: FRAG_SRC });
  return cachedCaps;
}

export function GpuFilmOverlay({
  getLayers,
  getMix,
  grade = NEUTRAL_GRADE,
  reducedMotion,
  jankRatio = 0,
  forceOff,
  className,
  style,
  capsOverride,
}: GpuFilmOverlayProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const compositorRef = useRef<WebGLCompositor | null>(null);
  const rafRef = useRef(0);
  const gradeRef = useRef(grade);
  gradeRef.current = grade;
  const [visible, setVisible] = useState(false);

  // Decide whether to run at all. Re-evaluated when the gating inputs change.
  const caps = getCaps(capsOverride);
  const decision = decideCompositor({
    caps,
    reducedMotion,
    jankRatio,
    forceOff,
    minTextureSize: 720,
  });

  useEffect(() => {
    if (!decision.useGpu) {
      setVisible(false);
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) return;
    const compositor = new WebGLCompositor(canvas);
    compositorRef.current = compositor;
    if (!compositor.init()) {
      compositorRef.current = null;
      setVisible(false);
      return;
    }

    let lastBoundOut: HTMLVideoElement | null = null;
    let lastBoundIn: HTMLVideoElement | null = null;
    const start = typeof performance !== "undefined" ? performance.now() : Date.now();

    const loop = () => {
      const c = compositorRef.current;
      if (!c || !c.isOperational) {
        setVisible(false);
        rafRef.current = requestAnimationFrame(loop);
        return;
      }
      const [outV, inV] = getLayers();
      if (outV !== lastBoundOut) {
        c.setLayerSource(0, outV ? videoSource(outV) : null);
        lastBoundOut = outV;
      }
      if (inV !== lastBoundIn) {
        c.setLayerSource(1, inV ? videoSource(inV) : null);
        lastBoundIn = inV;
      }
      // Size the buffer to the canvas's box, accounting for HiDPI.
      const w = canvas.clientWidth || canvas.width;
      const h = canvas.clientHeight || canvas.height;
      const dpr = typeof devicePixelRatio === "number" ? devicePixelRatio : 1;
      c.resize(w, h, dpr);

      const t = ((typeof performance !== "undefined" ? performance.now() : Date.now()) - start) / 1000;
      const drew = c.render({ mix: getMix(), grade: gradeRef.current, timeSeconds: t });
      // Reveal the GPU surface ONLY on a frame it actually drew; otherwise keep it
      // hidden so the CSS film shows through (no black frame from us).
      setVisible(drew);
      rafRef.current = requestAnimationFrame(loop);
    };
    rafRef.current = requestAnimationFrame(loop);

    return () => {
      cancelAnimationFrame(rafRef.current);
      compositor.dispose();
      compositorRef.current = null;
    };
    // getLayers/getMix are stable refs from the host; decision.useGpu gates mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decision.useGpu]);

  if (!decision.useGpu) return null;
  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      data-testid="gpu-film-overlay"
      className={className}
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        // Hidden until a real composited frame is drawn — the CSS film owns the
        // pixels underneath until then, upholding the no-black-frame guarantee.
        opacity: visible ? 1 : 0,
        pointerEvents: "none",
        ...style,
      }}
    />
  );
}

/** Reset the cached GPU probe — for tests that need to re-probe. */
export function __resetGpuProbeForTests(): void {
  cachedCaps = null;
}
