import { type PointerEvent, type RefObject, useRef, useState } from "react";

export interface CapturedRegion {
  dataUrl: string;
  rect: { x: number; y: number; w: number; h: number };
}

interface RegionSelectProps {
  videoRef: RefObject<HTMLVideoElement>;
  active: boolean;
  onRegion: (region: CapturedRegion) => void;
  onError?: (message: string) => void;
}

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * The Codex/Cursor-style region-select (kinora.md §5.4). Drag a box over the
 * frame; the client screenshots that region to a PNG via canvas. Accounts for
 * object-contain letterboxing so the captured pixels match what's on screen.
 */
export function RegionSelect({ videoRef, active, onRegion, onError }: RegionSelectProps) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const startRef = useRef<{ x: number; y: number } | null>(null);
  const [rect, setRect] = useState<Rect | null>(null);

  const localPoint = (e: PointerEvent) => {
    const r = overlayRef.current?.getBoundingClientRect();
    if (!r) return { x: 0, y: 0 };
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const onPointerDown = (e: PointerEvent) => {
    if (!active) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const p = localPoint(e);
    startRef.current = p;
    setRect({ x: p.x, y: p.y, w: 0, h: 0 });
  };

  const onPointerMove = (e: PointerEvent) => {
    if (!active || !startRef.current) return;
    const p = localPoint(e);
    const s = startRef.current;
    setRect({
      x: Math.min(s.x, p.x),
      y: Math.min(s.y, p.y),
      w: Math.abs(p.x - s.x),
      h: Math.abs(p.y - s.y),
    });
  };

  const onPointerUp = () => {
    if (!active || !rect || !startRef.current) return;
    startRef.current = null;
    if (rect.w < 8 || rect.h < 8) {
      setRect(null);
      return;
    }
    capture(rect);
  };

  const capture = (selection: Rect) => {
    const video = videoRef.current;
    const overlay = overlayRef.current;
    if (!video || !overlay) return;
    const natW = video.videoWidth;
    const natH = video.videoHeight;
    if (!natW || !natH) {
      onError?.("No video frame to capture yet.");
      return;
    }
    const ov = overlay.getBoundingClientRect();
    // object-contain letterbox mapping from overlay px → video px.
    const scale = Math.min(ov.width / natW, ov.height / natH);
    const dispW = natW * scale;
    const dispH = natH * scale;
    const offsetX = (ov.width - dispW) / 2;
    const offsetY = (ov.height - dispH) / 2;
    const vx = Math.max(0, Math.min(natW, (selection.x - offsetX) / scale));
    const vy = Math.max(0, Math.min(natH, (selection.y - offsetY) / scale));
    const vw = Math.max(1, Math.min(natW - vx, selection.w / scale));
    const vh = Math.max(1, Math.min(natH - vy, selection.h / scale));

    const canvas = document.createElement("canvas");
    canvas.width = Math.round(vw);
    canvas.height = Math.round(vh);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    try {
      ctx.drawImage(video, vx, vy, vw, vh, 0, 0, canvas.width, canvas.height);
      const dataUrl = canvas.toDataURL("image/png");
      onRegion({ dataUrl, rect: { x: vx, y: vy, w: vw, h: vh } });
    } catch {
      onError?.("Couldn't capture the frame (cross-origin video). The region note was not attached.");
    }
  };

  if (!active) return null;

  return (
    <div
      ref={overlayRef}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      className="absolute inset-0 z-20 cursor-crosshair"
    >
      {rect ? (
        <div
          className="absolute border-2 border-kinora-iris bg-kinora-glow/15"
          style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
        />
      ) : (
        <div className="absolute left-3 top-3 rounded-full bg-black/55 px-2.5 py-1 text-[0.7rem] text-white/80 backdrop-blur">
          Drag a box to comment on a region
        </div>
      )}
    </div>
  );
}
