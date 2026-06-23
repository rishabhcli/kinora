import { contentNormToElementRect, type ElementPctRect } from "@kinora/core";
import { type PointerEvent as ReactPointerEvent, useEffect, useRef, useState } from "react";

import { elementBoxToContentNorm, type NormBox, type PixelBox } from "./regionCapture";

interface RegionSelectOverlayProps {
  /** The stage's <video> — its displayed rect maps the drag to content coords. */
  video: HTMLVideoElement | null;
  /** Fires once a box is drawn, in normalized video-content coordinates. */
  onSelect: (box: NormBox) => void;
  /** Dismiss the overlay without selecting (Escape / click-through cancel). */
  onCancel: () => void;
}

const MIN_DRAG_PX = 6;

/**
 * The Codex/Cursor-style region selector (§5.4). An armed glass scrim over the
 * video stage: drag to box a detail, and on release the pixel rect is converted
 * to normalized content coordinates (letterbox-corrected) and reported up. The
 * selection dims everything outside it so the boxed subject reads clearly.
 */
export function RegionSelectOverlay({ video, onSelect, onCancel }: RegionSelectOverlayProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  const [rect, setRect] = useState<PixelBox | null>(null);

  // Take focus so Escape cancels without a click first.
  useEffect(() => {
    ref.current?.focus();
  }, []);

  function localPoint(event: ReactPointerEvent): { x: number; y: number } {
    const bounds = ref.current?.getBoundingClientRect();
    return { x: event.clientX - (bounds?.left ?? 0), y: event.clientY - (bounds?.top ?? 0) };
  }

  function onPointerDown(event: ReactPointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    ref.current?.setPointerCapture(event.pointerId);
    start.current = localPoint(event);
    setRect({ ...start.current, w: 0, h: 0 });
  }

  function onPointerMove(event: ReactPointerEvent): void {
    if (!start.current) return;
    const p = localPoint(event);
    setRect({
      x: Math.min(start.current.x, p.x),
      y: Math.min(start.current.y, p.y),
      w: Math.abs(p.x - start.current.x),
      h: Math.abs(p.y - start.current.y),
    });
  }

  function onPointerUp(event: ReactPointerEvent): void {
    const origin = start.current;
    start.current = null;
    ref.current?.releasePointerCapture?.(event.pointerId);
    if (!origin || !rect) return;
    if (rect.w < MIN_DRAG_PX || rect.h < MIN_DRAG_PX) {
      setRect(null); // a click, not a drag — ignore
      return;
    }
    const norm = video ? elementBoxToContentNorm(video, rect) : null;
    if (norm) onSelect(norm);
  }

  return (
    <div
      ref={ref}
      role="application"
      aria-label="Drag to select a region of the frame"
      className="absolute inset-0 z-20 cursor-crosshair touch-none select-none"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onKeyDown={(e) => e.key === "Escape" && onCancel()}
      tabIndex={-1}
    >
      {/* A faint scrim while nothing is boxed — signals the stage is armed. */}
      {!rect && <div className="absolute inset-0 bg-walnut-deep/30" />}

      {!rect && (
        <div className="pointer-events-none absolute inset-x-0 top-3 flex justify-center">
          <span className="rounded-full bg-black/55 px-3 py-1 text-[11px] font-medium text-white/85 ring-1 ring-white/15">
            Drag a box over the detail to direct
          </span>
        </div>
      )}

      {rect && (
        <div
          className="pointer-events-none absolute rounded-[3px] border border-ember-glow"
          style={{
            left: rect.x,
            top: rect.y,
            width: rect.w,
            height: rect.h,
            // Dim everything outside the selection (the classic region-select look).
            boxShadow: "0 0 0 9999px rgba(11,7,5,0.55)",
          }}
        >
          {/* Corner handles. */}
          {["-left-1 -top-1", "-right-1 -top-1", "-left-1 -bottom-1", "-right-1 -bottom-1"].map(
            (pos) => (
              <span
                key={pos}
                className={`absolute ${pos} h-2 w-2 rounded-[1px] bg-ember-glow shadow`}
              />
            ),
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The persistent marker for a bound region (§5.4): after a box is drawn it stays
 * on the stage so the Director keeps seeing what the note is attached to, until
 * they send or clear. Non-interactive (never blocks the transport). Positioned by
 * the tested inverse geometry (`contentNormToElementRect`), re-measured on resize
 * via a `ResizeObserver`, so it stays pinned to the subject for *any* clip aspect
 * — letterbox or pillarbox, not just a 16:9 clip filling the stage.
 */
export function RegionMarker({ video, box }: { video: HTMLVideoElement | null; box: NormBox }) {
  const [rect, setRect] = useState<ElementPctRect | null>(null);

  useEffect(() => {
    if (!video) return;
    const measure = (): void => {
      const r = video.getBoundingClientRect();
      setRect(contentNormToElementRect(r.width, r.height, video.videoWidth, video.videoHeight, box));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(video);
    return () => observer.disconnect();
  }, [video, box]);

  if (!rect) return null;
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute z-10 rounded-[3px] border-2 border-ember-glow/90 bg-ember/5"
      style={{
        left: `${rect.leftPct}%`,
        top: `${rect.topPct}%`,
        width: `${rect.widthPct}%`,
        height: `${rect.heightPct}%`,
      }}
    >
      <span className="absolute -top-1.5 left-0 -translate-y-full rounded bg-ember px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-walnut-deep shadow">
        Directing
      </span>
    </div>
  );
}
