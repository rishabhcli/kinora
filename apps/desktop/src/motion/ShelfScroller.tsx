import {
  useRef,
  useCallback,
  useEffect,
  useState,
  type ReactNode,
  type CSSProperties,
} from "react";
import { useMotion } from "./MotionProvider";

/**
 * <ShelfScroller> — the bookshelf rail: buttery inertial horizontal
 * motion with depth.
 *
 * What it adds on top of a native overflow rail (which already gives
 * trackpad momentum on macOS):
 *   • drag-to-scroll with physics-grade fling momentum (mouse users),
 *   • vertical-wheel → horizontal scroll, releasing to the page at the ends,
 *   • velocity-projected snap to the nearest cover after a fling,
 *   • a rubber-band when dragged past either end,
 *   • a horizontal depth-of-field mask so the rail recedes at its edges,
 *   • an optional parallax backdrop that drifts slower than the covers,
 *   • cheap painting at 100+ covers via content-visibility on each child.
 *
 * Agent 5 wraps real book rows: <ShelfScroller>{books.map(...)}</ShelfScroller>.
 * Reduced motion: momentum/rubber-band off, snap becomes an instant jump,
 * native scrolling still works.
 */

export interface ShelfScrollerProps {
  children: ReactNode;
  /** Snap to the nearest cover after a fling/scroll. Default true. */
  snap?: boolean;
  /** Convert vertical wheel to horizontal scroll. Default true. */
  wheelHorizontal?: boolean;
  /** Show hover arrow controls that nudge the rail. Default true. */
  arrows?: boolean;
  /** An optional layer rendered behind the covers, drifting at 0.5×. */
  backdrop?: ReactNode;
  className?: string;
  /** Class applied to the inner scrolling rail (gap, padding live here). */
  railClassName?: string;
  style?: CSSProperties;
  /** Gap between covers in px (also used to size snap math). Default 16. */
  gap?: number;
}

const FRICTION = 0.94; // per-frame velocity decay during fling
const MIN_VELOCITY = 0.02; // px/ms — below this the fling has stopped
const PROJECT_MS = 180; // how far ahead velocity projects the snap target
const DRAG_THRESHOLD = 6; // px of movement before a press becomes a drag
const RUBBER = 0.35; // resistance when dragging past an end

export function ShelfScroller({
  children,
  snap = true,
  wheelHorizontal = true,
  arrows = true,
  backdrop,
  className,
  railClassName,
  style,
  gap = 16,
}: ShelfScrollerProps) {
  const { reduced } = useMotion();
  const railRef = useRef<HTMLDivElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);

  // Imperative drag/fling state (no re-render on the hot path).
  const drag = useRef({
    active: false,
    moved: false,
    startX: 0,
    startScroll: 0,
    lastX: 0,
    lastT: 0,
    velocity: 0,
    rubber: 0,
  });
  const rafRef = useRef<number | null>(null);
  const wheelStop = useRef<number | null>(null);

  const [atStart, setAtStart] = useState(true);
  const [atEnd, setAtEnd] = useState(false);

  const cancelRaf = () => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
  };

  const updateEdges = useCallback(() => {
    const rail = railRef.current;
    if (!rail) return;
    const max = rail.scrollWidth - rail.clientWidth;
    setAtStart(rail.scrollLeft <= 1);
    setAtEnd(rail.scrollLeft >= max - 1);
    if (backdropRef.current) {
      const p = max > 0 ? rail.scrollLeft / max : 0;
      // Drift the backdrop at half-rate for parallax depth.
      backdropRef.current.style.transform = `translate3d(${-rail.scrollLeft * 0.5}px,0,0)`;
      backdropRef.current.style.setProperty("--mo-shelf-progress", p.toFixed(4));
    }
  }, []);

  /** Animate scrollLeft → target on a settle ease (used by snap + arrows). */
  const glideTo = useCallback(
    (target: number) => {
      const rail = railRef.current;
      if (!rail) return;
      const max = rail.scrollWidth - rail.clientWidth;
      const dest = Math.max(0, Math.min(max, target));
      if (reduced) {
        rail.scrollLeft = dest;
        updateEdges();
        return;
      }
      cancelRaf();
      const from = rail.scrollLeft;
      const dist = dest - from;
      const duration = Math.min(620, 180 + Math.abs(dist) * 0.6);
      const start = performance.now();
      const ease = (t: number) => 1 - Math.pow(1 - t, 3); // ease-out cubic
      const step = (now: number) => {
        const t = Math.min(1, (now - start) / duration);
        rail.scrollLeft = from + dist * ease(t);
        updateEdges();
        if (t < 1) rafRef.current = requestAnimationFrame(step);
        else rafRef.current = null;
      };
      rafRef.current = requestAnimationFrame(step);
    },
    [reduced, updateEdges],
  );

  /** Find the nearest child boundary to a scroll position. */
  const nearestSnap = useCallback((scrollPos: number) => {
    const rail = railRef.current;
    if (!rail) return scrollPos;
    const padLeft = parseFloat(getComputedStyle(rail).paddingLeft) || 0;
    let best = scrollPos;
    let bestDist = Infinity;
    for (const child of Array.from(rail.children) as HTMLElement[]) {
      const target = child.offsetLeft - padLeft;
      const d = Math.abs(target - scrollPos);
      if (d < bestDist) {
        bestDist = d;
        best = target;
      }
    }
    return best;
  }, []);

  const doSnap = useCallback(
    (velocity = 0) => {
      if (!snap) return;
      const rail = railRef.current;
      if (!rail) return;
      const projected = rail.scrollLeft + velocity * PROJECT_MS;
      glideTo(nearestSnap(projected));
    },
    [snap, glideTo, nearestSnap],
  );

  // — Fling momentum after a drag release —
  const startMomentum = useCallback(() => {
    const rail = railRef.current;
    if (!rail || reduced) {
      doSnap(drag.current.velocity);
      return;
    }
    cancelRaf();
    let last = performance.now();
    const step = (now: number) => {
      const dt = now - last;
      last = now;
      const d = drag.current;
      d.velocity *= FRICTION;
      rail.scrollLeft -= d.velocity * dt;
      updateEdges();
      const max = rail.scrollWidth - rail.clientWidth;
      const hitEdge = rail.scrollLeft <= 0 || rail.scrollLeft >= max;
      if (Math.abs(d.velocity) > MIN_VELOCITY && !hitEdge) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        rafRef.current = null;
        doSnap(d.velocity);
      }
    };
    rafRef.current = requestAnimationFrame(step);
  }, [reduced, doSnap, updateEdges]);

  // — Pointer drag —
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    // Ignore non-primary buttons; let native scrollbars work.
    if (e.button !== 0) return;
    cancelRaf();
    const rail = railRef.current!;
    const d = drag.current;
    d.active = true;
    d.moved = false;
    d.startX = e.clientX;
    d.startScroll = rail.scrollLeft;
    d.lastX = e.clientX;
    d.lastT = performance.now();
    d.velocity = 0;
    d.rubber = 0;
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d.active) return;
    const rail = railRef.current!;
    const dx = e.clientX - d.startX;
    if (!d.moved && Math.abs(dx) < DRAG_THRESHOLD) return; // still a click
    if (!d.moved) {
      d.moved = true;
      rail.setPointerCapture?.(e.pointerId);
      rail.classList.add("mo-grabbing");
    }

    const max = rail.scrollWidth - rail.clientWidth;
    let next = d.startScroll - dx;
    // Rubber-band past the ends (drag, transform the rail back).
    d.rubber = 0;
    if (next < 0) {
      d.rubber = reduced ? 0 : -next * RUBBER;
      next = 0;
    } else if (next > max) {
      d.rubber = reduced ? 0 : -(next - max) * RUBBER;
      next = max;
    }
    rail.scrollLeft = next;
    rail.style.transform = d.rubber ? `translate3d(${d.rubber}px,0,0)` : "";

    // Velocity sample (px/ms), lightly smoothed.
    const now = performance.now();
    const dt = now - d.lastT || 16;
    const v = (e.clientX - d.lastX) / dt;
    d.velocity = d.velocity * 0.6 + v * 0.4;
    d.lastX = e.clientX;
    d.lastT = now;
    updateEdges();
  };

  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d.active) return;
    d.active = false;
    const rail = railRef.current!;
    rail.classList.remove("mo-grabbing");
    rail.releasePointerCapture?.(e.pointerId);
    // Release the rubber-band back to rest.
    if (d.rubber) {
      rail.style.transition = "transform 0.4s cubic-bezier(0.22,1,0.36,1)";
      rail.style.transform = "";
      window.setTimeout(() => {
        if (rail) rail.style.transition = "";
      }, 420);
      d.rubber = 0;
    }
    if (d.moved) startMomentum();
  };

  // Suppress the click that follows a real drag so it doesn't open a book.
  const onClickCapture = (e: React.MouseEvent) => {
    if (drag.current.moved) {
      e.stopPropagation();
      e.preventDefault();
      drag.current.moved = false;
    }
  };

  // — Wheel: vertical → horizontal, releasing to the page at the ends.
  // Attached natively as a NON-passive listener (React's onWheel is passive,
  // so preventDefault there is a no-op) so the scroll-jack actually works. —
  const handleWheel = useCallback(
    (e: WheelEvent) => {
      if (!wheelHorizontal) return;
      const rail = railRef.current;
      if (!rail) return;
      const max = rail.scrollWidth - rail.clientWidth;
      if (max <= 0) return;
      const delta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      const atLeft = rail.scrollLeft <= 0 && delta < 0;
      const atRight = rail.scrollLeft >= max && delta > 0;
      if (atLeft || atRight) return; // let the page scroll past the shelf
      e.preventDefault();
      cancelRaf();
      rail.scrollLeft = Math.max(0, Math.min(max, rail.scrollLeft + delta));
      updateEdges();
      if (wheelStop.current) window.clearTimeout(wheelStop.current);
      wheelStop.current = window.setTimeout(() => doSnap(0), 120);
    },
    [wheelHorizontal, updateEdges, doSnap],
  );

  const nudge = (dir: 1 | -1) => {
    const rail = railRef.current;
    if (!rail) return;
    glideTo(rail.scrollLeft + dir * rail.clientWidth * 0.8);
  };

  useEffect(() => {
    updateEdges();
    const rail = railRef.current;
    if (!rail) return;
    const onScroll = () => updateEdges();
    const onResize = () => updateEdges();
    rail.addEventListener("scroll", onScroll, { passive: true });
    rail.addEventListener("wheel", handleWheel, { passive: false });
    window.addEventListener("resize", onResize);
    return () => {
      rail.removeEventListener("scroll", onScroll);
      rail.removeEventListener("wheel", handleWheel);
      window.removeEventListener("resize", onResize);
      cancelRaf();
      if (wheelStop.current) window.clearTimeout(wheelStop.current);
    };
  }, [updateEdges, handleWheel, children]);

  return (
    <div className={`relative ${className ?? ""}`} style={style}>
      {backdrop != null && (
        <div
          ref={backdropRef}
          aria-hidden
          className="pointer-events-none absolute inset-0 z-0 will-change-transform"
        >
          {backdrop}
        </div>
      )}

      <div
        ref={railRef}
        className={`mo-shelf-rail mo-edge-fade-x mo-grab relative z-10 flex overflow-x-auto overflow-y-hidden ${railClassName ?? ""}`}
        style={{
          gap,
          scrollbarWidth: "none",
          msOverflowStyle: "none",
          WebkitOverflowScrolling: "touch",
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onClickCapture={onClickCapture}
      >
        {children}
      </div>

      {arrows && (
        <>
          <ShelfArrow side="left" hidden={atStart} onClick={() => nudge(-1)} />
          <ShelfArrow side="right" hidden={atEnd} onClick={() => nudge(1)} />
        </>
      )}
    </div>
  );
}

function ShelfArrow({
  side,
  hidden,
  onClick,
}: {
  side: "left" | "right";
  hidden: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={side === "left" ? "Scroll left" : "Scroll right"}
      onClick={onClick}
      tabIndex={hidden ? -1 : 0}
      className="mo-hover absolute top-1/2 z-20 grid h-9 w-9 -translate-y-1/2 place-items-center rounded-full"
      style={{
        [side]: 4,
        background: "rgba(15, 14, 12, 0.72)",
        border: "1px solid rgba(255,255,255,0.1)",
        color: "rgba(232,226,216,0.92)",
        opacity: hidden ? 0 : 1,
        pointerEvents: hidden ? "none" : "auto",
        transition: "opacity var(--mo-t-base, 0.32s) var(--mo-ease-standard)",
        backdropFilter: "blur(6px)",
        WebkitBackdropFilter: "blur(6px)",
      }}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        {side === "left" ? <path d="M15 18l-6-6 6-6" /> : <path d="M9 18l6-6-6-6" />}
      </svg>
    </button>
  );
}

export default ShelfScroller;
