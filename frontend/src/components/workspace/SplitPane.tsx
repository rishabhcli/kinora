import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

interface SplitPaneProps {
  left: ReactNode;
  right: ReactNode;
  initialRatio?: number;
  minRatio?: number;
  maxRatio?: number;
}

/**
 * Two panes with a draggable divider. Horizontal on wide viewports, stacked on
 * narrow ones. The ratio is the left pane's fraction of the container width.
 */
export function SplitPane({
  left,
  right,
  initialRatio = 0.5,
  minRatio = 0.28,
  maxRatio = 0.72,
}: SplitPaneProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [ratio, setRatio] = useState(initialRatio);
  const [dragging, setDragging] = useState(false);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    setDragging(true);
  }, []);

  useEffect(() => {
    if (!dragging) return undefined;
    const onMove = (e: PointerEvent) => {
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const next = (e.clientX - rect.left) / rect.width;
      setRatio(Math.min(maxRatio, Math.max(minRatio, next)));
    };
    const onUp = () => setDragging(false);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragging, minRatio, maxRatio]);

  return (
    <div
      ref={containerRef}
      className={`flex h-full w-full flex-col overflow-hidden md:flex-row ${
        dragging ? "select-none" : ""
      }`}
    >
      <div className="min-h-0 min-w-0 flex-1 md:flex-none" style={{ flexBasis: `${ratio * 100}%` }}>
        {left}
      </div>
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize panes"
        onPointerDown={onPointerDown}
        className="group hidden w-1.5 shrink-0 cursor-col-resize items-center justify-center bg-kinora-line/60 transition-colors hover:bg-kinora-iris/50 md:flex"
      >
        <span className="h-10 w-0.5 rounded-full bg-white/20 transition-colors group-hover:bg-white/50" />
      </div>
      <div className="min-h-0 min-w-0 flex-1">{right}</div>
    </div>
  );
}
