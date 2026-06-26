import { useRef, ReactNode } from "react";
import { useReducedMotion } from "framer-motion";

export function CometCard({
  children,
  rotateDepth = 17.5,
  translateDepth = 20,
  glare = true,
  className,
}: {
  children: ReactNode;
  rotateDepth?: number;
  translateDepth?: number;
  glare?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const childRef = useRef<HTMLDivElement>(null);
  const glareRef = useRef<HTMLDivElement>(null);
  const reduce = useReducedMotion();

  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el || reduce) return;
    const rect = el.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width - 0.5;
    const py = (e.clientY - rect.top) / rect.height - 0.5;
    el.style.setProperty("--rx", `${-py * rotateDepth}deg`);
    el.style.setProperty("--ry", `${px * rotateDepth}deg`);
    el.style.setProperty("--tz", `${translateDepth}px`);
    // Feed the cursor position (as %) to the glare overlay so the highlight
    // tracks the pointer across the cover.
    el.style.setProperty("--mx", `${(px + 0.5) * 100}%`);
    el.style.setProperty("--my", `${(py + 0.5) * 100}%`);
  };

  return (
    <div
      ref={ref}
      onMouseMove={handleMouseMove}
      onMouseEnter={() => {
        if (reduce) return;
        if (ref.current) {
          ref.current.style.transformStyle = "preserve-3d";
          ref.current.style.transition = "transform 0.2s ease-out";
          ref.current.style.transform = "perspective(1400px) rotateX(var(--rx,0deg)) rotateY(var(--ry,0deg))";
        }
        if (childRef.current) {
          childRef.current.style.transformStyle = "preserve-3d";
          childRef.current.style.transition = "transform 0.2s ease-out";
          childRef.current.style.transform = "translateZ(var(--tz,0px))";
        }
        if (glareRef.current) glareRef.current.style.opacity = "1";
      }}
      onMouseLeave={() => {
        const el = ref.current;
        if (!el) return;
        if (glareRef.current) glareRef.current.style.opacity = "0";
        el.style.setProperty("--rx", "0deg");
        el.style.setProperty("--ry", "0deg");
        el.style.setProperty("--tz", "0px");
        el.style.transformStyle = "flat";
        el.style.transition = "transform 0.2s ease-out";
        if (childRef.current) {
          childRef.current.style.transformStyle = "flat";
          childRef.current.style.transition = "transform 0.2s ease-out";
        }
        // Remove transform and transition after animation completes
        setTimeout(() => {
          if (ref.current) {
            ref.current.style.transition = "";
            ref.current.style.transform = "";
          }
          if (childRef.current) {
            childRef.current.style.transition = "";
            childRef.current.style.transform = "";
          }
        }, 250);
      }}
      className={className}
    >
      <div ref={childRef} style={{ position: "relative" }}>
        {children}
        {glare && <div ref={glareRef} className="comet-glare" aria-hidden="true" />}
      </div>
    </div>
  );
}
