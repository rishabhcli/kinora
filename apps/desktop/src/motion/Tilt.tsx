import { useRef, type ReactNode, type CSSProperties } from "react";
import { useMotion } from "./MotionProvider";

/**
 * Tilt — the 3D cover-hover primitive (generalised from `CometCard`).
 *
 * A pointer-tracking parallax tilt: the surface rotates toward the cursor
 * and its content lifts on the Z axis, with an optional specular glare
 * that tracks the pointer. Written with direct style writes (not React
 * state) so a fast mouse never triggers a re-render — 60fps by design.
 *
 * Accessibility:
 *   • reduced motion  → the tilt is inert (no rotation, no lift).
 *   • reduced transparency → the glare film is hidden (CSS, .mo-tilt-glare).
 */

export interface TiltOptions {
  /** Max rotation in degrees at the edges. Default 12. */
  rotateDepth?: number;
  /** Z-lift of the inner content in px. Default 15. */
  translateDepth?: number;
  /** Perspective depth in px. Default 1100. */
  perspective?: number;
}

const SETTLE = "transform 0.18s cubic-bezier(0.22, 1, 0.36, 1)";

/**
 * useTilt — refs + handlers for a tilt surface. Spread `bind` on the
 * outer element, attach `innerRef` to the lifting layer, and render the
 * returned `glare` element inside it.
 */
export function useTilt(opts: TiltOptions = {}) {
  const { rotateDepth = 12, translateDepth = 15, perspective = 1100 } = opts;
  const ref = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const glareRef = useRef<HTMLDivElement>(null);
  const { reduced } = useMotion();

  const onMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el || reduced) return;
    const rect = el.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width - 0.5;
    const py = (e.clientY - rect.top) / rect.height - 0.5;
    el.style.setProperty("--mo-rx", `${-py * rotateDepth}deg`);
    el.style.setProperty("--mo-ry", `${px * rotateDepth}deg`);
    if (glareRef.current) {
      glareRef.current.style.setProperty("--mo-mx", `${(px + 0.5) * 100}%`);
      glareRef.current.style.setProperty("--mo-my", `${(py + 0.5) * 100}%`);
    }
  };

  const onMouseEnter = () => {
    if (reduced) return;
    const el = ref.current;
    if (el) {
      el.style.transformStyle = "preserve-3d";
      el.style.transition = SETTLE;
      el.style.transform = `perspective(${perspective}px) rotateX(var(--mo-rx,0deg)) rotateY(var(--mo-ry,0deg))`;
    }
    if (innerRef.current) {
      innerRef.current.style.transformStyle = "preserve-3d";
      innerRef.current.style.transition = SETTLE;
      innerRef.current.style.transform = `translateZ(${translateDepth}px)`;
    }
    if (glareRef.current) glareRef.current.style.opacity = "1";
  };

  const onMouseLeave = () => {
    const el = ref.current;
    if (!el) return;
    if (glareRef.current) glareRef.current.style.opacity = "0";
    el.style.setProperty("--mo-rx", "0deg");
    el.style.setProperty("--mo-ry", "0deg");
    el.style.transition = SETTLE;
    el.style.transform = `perspective(${perspective}px) rotateX(0deg) rotateY(0deg)`;
    if (innerRef.current) {
      innerRef.current.style.transition = SETTLE;
      innerRef.current.style.transform = "translateZ(0px)";
    }
    // Clear inline transforms once settled so they don't fight layout.
    window.setTimeout(() => {
      if (ref.current) {
        ref.current.style.transition = "";
        ref.current.style.transform = "";
        ref.current.style.transformStyle = "flat";
      }
      if (innerRef.current) {
        innerRef.current.style.transition = "";
        innerRef.current.style.transform = "";
      }
    }, 220);
  };

  return {
    ref,
    innerRef,
    glareRef,
    reduced,
    bind: { onMouseMove, onMouseEnter, onMouseLeave },
  };
}

export interface TiltProps extends TiltOptions {
  children: ReactNode;
  /** Render the specular glare overlay. Default true. */
  glare?: boolean;
  className?: string;
  style?: CSSProperties;
}

/** <Tilt> — convenience wrapper around `useTilt`. */
export function Tilt({
  children,
  glare = true,
  rotateDepth,
  translateDepth,
  perspective,
  className,
  style,
}: TiltProps) {
  const { ref, innerRef, glareRef, reduced, bind } = useTilt({
    rotateDepth,
    translateDepth,
    perspective,
  });

  return (
    <div ref={ref} className={className} style={style} {...bind}>
      <div ref={innerRef} style={{ position: "relative" }}>
        {children}
        {glare && !reduced && (
          <div ref={glareRef} className="mo-tilt-glare" aria-hidden="true" />
        )}
      </div>
    </div>
  );
}

export default Tilt;
