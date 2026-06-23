import { type BufferHealthSummary, type BufferPoint } from "@kinora/core";
import { useRef, useState } from "react";

import { downloadBlob, svgToPng } from "./export";

const VIEW_W = 760;
const VIEW_H = 240;
const PAD = { left: 38, right: 58, top: 16, bottom: 26 };
const PLOT_W = VIEW_W - PAD.left - PAD.right;
const PLOT_H = VIEW_H - PAD.top - PAD.bottom;

/** Slider default shown while velocity is "auto" (the session's own pace). */
const AUTO_VELOCITY_HINT = 3.5;

interface ChartProps {
  trace: BufferPoint[];
  health: BufferHealthSummary;
  isLoading: boolean;
  isFetching: boolean;
  isError: boolean;
  sessionReady: boolean;
  aboveLowTarget: number;
  onRefresh: () => void;
  /** Reading speed override fed to the sim (`null` = the session's own pace). */
  velocity: number | null;
  onVelocityChange: (v: number | null) => void;
}

function ControlButton({
  onClick,
  disabled,
  spinning,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  spinning?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="no-drag inline-flex items-center gap-1.5 rounded-full bg-white/8 px-3 py-1.5 text-[11px] font-medium text-white/80 transition hover:bg-white/16 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow disabled:opacity-50"
    >
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={spinning ? "animate-spin" : ""}
      >
        <path d="M21 12a9 9 0 1 1-2.64-6.36M21 4v5h-5" />
      </svg>
      {children}
    </button>
  );
}

/** A compact reading-speed control: drives the sim's `velocity` param so the
 *  sawtooth visibly tightens (faster reading drains the buffer) or relaxes. */
function VelocityControl({
  velocity,
  onVelocityChange,
}: {
  velocity: number | null;
  onVelocityChange: (v: number | null) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[10.5px] uppercase tracking-wider text-white/40">Speed</span>
      <input
        type="range"
        min={1}
        max={12}
        step={0.5}
        value={velocity ?? AUTO_VELOCITY_HINT}
        aria-label="Reading speed (words per second)"
        onChange={(e) => onVelocityChange(Number(e.target.value))}
        className={`h-1.5 w-24 cursor-pointer appearance-none rounded-full bg-white/15 accent-ember-glow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow/60 ${
          velocity === null ? "opacity-50" : ""
        }`}
      />
      <span className="w-[3.6rem] text-[11px] tabular-nums text-white/60">
        {velocity === null ? "auto" : `${velocity.toFixed(1)} wps`}
      </span>
      <button
        type="button"
        onClick={() => onVelocityChange(null)}
        disabled={velocity === null}
        className="no-drag rounded-full bg-white/8 px-2 py-1 text-[10.5px] font-medium text-white/75 transition hover:bg-white/16 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow disabled:opacity-40"
      >
        Auto
      </button>
    </div>
  );
}

function Placeholder({ children }: { children: string }) {
  return (
    <div className="flex h-[240px] items-center justify-center rounded-xl border border-white/8 bg-white/[0.02] text-[12px] text-white/45">
      {children}
    </div>
  );
}

/**
 * The §4.10 committed-buffer occupancy sawtooth, recomputed live for the current
 * reading session (zero video-seconds). The ember area is the committed seconds
 * buffered ahead of the reader; the dashed emerald/rose lines are the high/low
 * watermarks. A healthy run stays above `L` and shows clean burst-then-idle
 * teeth — the visual proof that generation-on-scroll keeps pace. The speed
 * slider re-runs the sim so a judge can watch the hysteresis respond; hover the
 * plot to read exact values; and it exports to PNG for the deck.
 */
export function BufferSawtoothChart({
  trace,
  health,
  isLoading,
  isFetching,
  isError,
  sessionReady,
  aboveLowTarget,
  onRefresh,
  velocity,
  onVelocityChange,
}: ChartProps) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hover, setHover] = useState<{ index: number; xFrac: number } | null>(null);
  const [exporting, setExporting] = useState(false);

  const exportPng = () => {
    if (!svgRef.current) return;
    setExporting(true);
    void svgToPng(svgRef.current)
      .then((blob) => downloadBlob(blob, "kinora-buffer-sawtooth.png"))
      .catch(() => undefined)
      .finally(() => setExporting(false));
  };

  const header = (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <p className="text-[11px] text-white/45">
        Recomputed from your current position · zero video-seconds spent
      </p>
      <div className="flex flex-wrap items-center gap-2.5">
        <VelocityControl velocity={velocity} onVelocityChange={onVelocityChange} />
        <ControlButton onClick={onRefresh} disabled={isFetching} spinning={isFetching}>
          {isFetching ? "Recomputing…" : "Recompute"}
        </ControlButton>
      </div>
    </div>
  );

  if (!sessionReady) {
    return (
      <div>
        {header}
        <Placeholder>Start reading to trace this session’s committed buffer.</Placeholder>
      </div>
    );
  }
  if (isLoading) {
    return (
      <div>
        {header}
        <div className="shimmer h-[240px] rounded-xl border border-white/8 bg-white/[0.02]" />
      </div>
    );
  }
  if (isError) {
    return (
      <div>
        {header}
        <Placeholder>Could not recompute the buffer trace — try again.</Placeholder>
      </div>
    );
  }
  if (trace.length < 2) {
    return (
      <div>
        {header}
        <Placeholder>Not enough samples yet to draw the sawtooth.</Placeholder>
      </div>
    );
  }

  // Scales. The committed curve and both watermark series share one y-domain.
  const tMax = Math.max(...trace.map((p) => p.t), 1);
  const maxCommitted = Math.max(...trace.map((p) => p.committed_seconds_ahead));
  const maxHigh = Math.max(...trace.map((p) => p.high));
  const yMax = Math.max(maxCommitted, maxHigh, 1) * 1.12;

  const x = (t: number) => PAD.left + (t / tMax) * PLOT_W;
  const y = (v: number) => PAD.top + (1 - v / yMax) * PLOT_H;

  const baseY = y(0);
  const committed = trace.map((p) => ({ cx: x(p.t), cy: y(p.committed_seconds_ahead) }));
  const first = committed.at(0);
  const lastC = committed.at(-1);
  const last = trace.at(-1);
  if (!first || !lastC || !last) return null; // unreachable: guarded by trace.length < 2

  const committedPts = committed.map((p) => `${p.cx},${p.cy}`).join(" ");
  const lowPts = trace.map((p) => `${x(p.t)},${y(p.low)}`).join(" ");
  const highPts = trace.map((p) => `${x(p.t)},${y(p.high)}`).join(" ");
  const areaPath = [
    `M ${first.cx},${baseY}`,
    ...committed.map((p) => `L ${p.cx},${p.cy}`),
    `L ${lastC.cx},${baseY}`,
    "Z",
  ].join(" ");

  const aboveLowPct = health.fractionAboveLow * 100;
  const healthy = health.fractionAboveLow >= aboveLowTarget && health.stalls === 0;
  const xTicks = [0, tMax / 2, tMax];
  const axisColor = "rgba(255,255,255,0.4)";

  // Hover → nearest sample (samples are dense, so a linear scan on t is fine).
  const onMove = (event: React.MouseEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const xFrac = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const t = xFrac * tMax;
    let bestIndex = 0;
    let bestDist = Infinity;
    for (let i = 0; i < trace.length; i++) {
      const p = trace[i];
      if (!p) continue;
      const d = Math.abs(p.t - t);
      if (d < bestDist) {
        bestDist = d;
        bestIndex = i;
      }
    }
    setHover({ index: bestIndex, xFrac });
  };

  const hovered = hover ? trace[hover.index] : undefined;
  const hoveredPt = hover ? committed[hover.index] : undefined;

  return (
    <div>
      {header}
      <div className="relative" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <svg
          ref={svgRef}
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          className="h-auto w-full"
          role="img"
          aria-label={`Committed-buffer occupancy over ${Math.round(tMax)} seconds of reading; ${aboveLowPct.toFixed(0)} percent above the low watermark, ${health.stalls} stalls`}
        >
          <defs>
            <linearGradient id="bufferFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#f4a85d" stopOpacity="0.42" />
              <stop offset="100%" stopColor="#e0863a" stopOpacity="0.04" />
            </linearGradient>
          </defs>

          {/* Baseline (empty buffer) */}
          <line x1={PAD.left} y1={baseY} x2={VIEW_W - PAD.right} y2={baseY} stroke="rgba(255,255,255,0.18)" strokeWidth="1" />

          {/* x-axis ticks + time labels */}
          {xTicks.map((t, i) => (
            <text
              key={i}
              x={x(t)}
              y={VIEW_H - 8}
              textAnchor={i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle"}
              fill={axisColor}
              fontSize="10"
            >
              {Math.round(t)}s
            </text>
          ))}

          {/* High watermark (H) — the burst ceiling */}
          <polyline points={highPts} fill="none" stroke="#34d399" strokeWidth="1.4" strokeDasharray="5 4" vectorEffect="non-scaling-stroke" opacity="0.85" />
          <text x={VIEW_W - PAD.right + 6} y={y(last.high) + 3} fill="#6ee7b7" fontSize="10">
            H {Math.round(last.high)}s
          </text>

          {/* Low watermark (L) — the refill trigger / danger floor */}
          <polyline points={lowPts} fill="none" stroke="#fb7185" strokeWidth="1.4" strokeDasharray="5 4" vectorEffect="non-scaling-stroke" opacity="0.85" />
          <text x={VIEW_W - PAD.right + 6} y={y(last.low) + 3} fill="#fda4af" fontSize="10">
            L {Math.round(last.low)}s
          </text>

          {/* Committed buffer — the sawtooth itself */}
          <path d={areaPath} fill="url(#bufferFill)" />
          <polyline points={committedPts} fill="none" stroke="#f4a85d" strokeWidth="2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />

          {/* Hover crosshair + point marker */}
          {hovered && hoveredPt && (
            <>
              <line x1={x(hovered.t)} y1={PAD.top} x2={x(hovered.t)} y2={baseY} stroke="rgba(255,255,255,0.35)" strokeWidth="1" strokeDasharray="3 3" />
              <circle cx={hoveredPt.cx} cy={hoveredPt.cy} r="3.5" fill="#f4a85d" stroke="#160e08" strokeWidth="1.5" />
            </>
          )}

          {/* y label */}
          <text x={PAD.left} y={PAD.top - 4} fill={axisColor} fontSize="10">
            committed s ahead
          </text>
        </svg>

        {/* Hover tooltip (HTML, positioned along the plot) */}
        {hover && hovered && (
          <div
            className="pointer-events-none absolute top-1 z-10 -translate-x-1/2 rounded-lg border border-white/12 bg-walnut-deep/95 px-2.5 py-1.5 text-[11px] tabular-nums text-white shadow-lg"
            style={{ left: `${Math.max(8, Math.min(92, hover.xFrac * 100))}%` }}
          >
            <div className="text-white/55">t = {hovered.t.toFixed(1)}s</div>
            <div className="font-semibold text-ember-glow">{hovered.committed_seconds_ahead.toFixed(1)}s buffered</div>
            <div className="text-white/45">
              L {Math.round(hovered.low)} · H {Math.round(hovered.high)}
            </div>
          </div>
        )}
      </div>

      {/* Buffer-health chips (the §13 verdict over the trace) + PNG export */}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
        <span
          className={`rounded-full px-2.5 py-1 font-medium tabular-nums ${
            health.fractionAboveLow >= aboveLowTarget ? "bg-emerald-400/15 text-emerald-300" : "bg-amber-400/15 text-amber-300"
          }`}
        >
          {aboveLowPct.toFixed(1)}% above L
        </span>
        <span
          className={`rounded-full px-2.5 py-1 font-medium tabular-nums ${
            health.stalls === 0 ? "bg-emerald-400/15 text-emerald-300" : "bg-rose-400/15 text-rose-300"
          }`}
        >
          {health.stalls} stall{health.stalls === 1 ? "" : "s"}
        </span>
        <span className="rounded-full bg-white/8 px-2.5 py-1 text-white/60 tabular-nums">
          {Math.round(health.durationS)}s traced
        </span>
        <span className={`text-[11px] ${healthy ? "text-emerald-300/80" : "text-amber-300/80"}`}>
          {healthy ? "Hysteresis holding — no stalls" : "Watch the floor — buffer dipped near L"}
        </span>
        <button
          type="button"
          onClick={exportPng}
          disabled={exporting}
          className="no-drag ml-auto inline-flex items-center gap-1.5 rounded-full bg-white/8 px-3 py-1 font-medium text-white/80 transition hover:bg-white/16 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow disabled:opacity-50"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <path d="m21 15-5-5L5 21" />
            <circle cx="9" cy="9" r="1.6" />
          </svg>
          {exporting ? "Exporting…" : "PNG"}
        </button>
      </div>
    </div>
  );
}
