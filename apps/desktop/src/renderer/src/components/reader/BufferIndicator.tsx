import {
  type BeatStage,
  type BufferPoint,
  type BufferZone,
  advanceSawtoothCursor,
  bufferFraction,
  bufferHealth,
  classifyBufferSurface,
  isReaderActive,
  queryKeys,
  sampleSawtoothAt,
  STAGE_NOTICE,
} from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../lib/api";

/**
 * The §5.3 buffer indicator — the *only* surfacing of the generation machinery a
 * plain reader ever sees: a faint hairline along the top edge of the video stage
 * that fills toward the high watermark `H`, breathing during a real generation
 * burst and resting quietly in the `[L, H)` idle band. A small zone badge names
 * what's ahead ("Full film" / "Preview still" / "Planning ahead"), flips to
 * "Catching up" if the reader outruns the render (§4.11), and warms to amber under
 * budget pressure with a notice tied to the §12.4 ladder rung. A persisted debug
 * toggle expands a compact §4.10 sawtooth + buffer-health verdict (zero
 * video-seconds — the §13 proof).
 *
 * Occupancy is driven by the live `buffer_state` event when real video is being
 * generated; otherwise (the default, live-gate-off build) it plays the recomputed
 * buffer-trace sawtooth, velocity-matched to the reader, along a cursor that
 * advances while reading and holds while idle or while the window is hidden. All
 * the buffer math is shared, tested core (`@kinora/core` `sync/buffer`).
 */
const DEBUG_KEY = "kinora.buffer.debug.v1";

/** Structural shape of the live buffer event (decoupled from the hook export). */
export interface BufferView {
  committedAheadS: number;
  low: number;
  high: number;
  commitHorizon: number;
  bursting: boolean;
  idle: boolean;
  zone: BufferZone;
  etaNextS: number | null;
  velocityWps: number | null;
  inflightCommitted: number;
  inflightSpeculative: number;
  promoted: number;
  budgetRemainingS: number | null;
}

interface BufferIndicatorProps {
  sessionId: string | null;
  bufferState: BufferView | null;
  focusWord: number;
  velocity: number;
  stage: BeatStage;
  budgetLow: boolean;
}

const ZONE_TONE: Record<BufferZone, string> = {
  committed: "text-ember-glow",
  speculative: "text-amber-300",
  cold: "text-white/55",
};

/** Coarse velocity buckets so the velocity-matched trace refetches rarely (§4.3). */
type VelocityBucket = "slow" | "normal" | "fast";
function velocityBucket(v: number): VelocityBucket {
  const a = Math.abs(v);
  return a < 3 ? "slow" : a > 8 ? "fast" : "normal";
}
function bucketVelocity(bucket: VelocityBucket): number | undefined {
  return bucket === "slow" ? 2.5 : bucket === "fast" ? 10 : undefined;
}

function loadDebug(): boolean {
  try {
    return localStorage.getItem(DEBUG_KEY) === "1";
  } catch {
    return false;
  }
}

/** A compact inline sawtooth (the §4.10 buffer-trace) with L/H lines + a cursor. */
function MiniSawtooth({ trace, cursorT }: { trace: BufferPoint[]; cursorT: number }) {
  const W = 184;
  const H = 40;
  const lastPoint = trace[trace.length - 1];
  if (trace.length < 2 || !lastPoint) {
    return <div className="h-[40px] w-[184px] rounded bg-white/[0.03]" />;
  }
  const tMax = Math.max(lastPoint.t, 1);
  const yMax = Math.max(...trace.map((p) => p.high), ...trace.map((p) => p.committed_seconds_ahead), 1) * 1.1;
  const x = (t: number) => (t / tMax) * W;
  const y = (v: number) => H - (v / yMax) * H;
  const line = trace.map((p) => `${x(p.t).toFixed(1)},${y(p.committed_seconds_ahead).toFixed(1)}`).join(" ");
  const area = `M0,${H} ${line} ${x(tMax).toFixed(1)},${H} Z`;
  const lowY = y(lastPoint.low);
  const highY = y(lastPoint.high);
  const cx = x(Math.max(0, Math.min(cursorT, tMax)));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-[40px] w-[184px]" role="img" aria-label="Committed-buffer sawtooth">
      <path d={area} fill="rgba(244,168,93,0.16)" />
      <polyline points={line} fill="none" stroke="#f4a85d" strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
      <line x1="0" y1={highY} x2={W} y2={highY} stroke="#34d399" strokeWidth="1" strokeDasharray="3 3" opacity="0.7" vectorEffect="non-scaling-stroke" />
      <line x1="0" y1={lowY} x2={W} y2={lowY} stroke="#fb7185" strokeWidth="1" strokeDasharray="3 3" opacity="0.7" vectorEffect="non-scaling-stroke" />
      <line x1={cx} y1="0" x2={cx} y2={H} stroke="rgba(255,255,255,0.6)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function BufferIndicator({
  sessionId,
  bufferState,
  focusWord,
  velocity,
  stage,
  budgetLow,
}: BufferIndicatorProps) {
  const [debug, setDebug] = useState(loadDebug);
  const toggleDebug = (): void =>
    setDebug((v) => {
      const next = !v;
      try {
        localStorage.setItem(DEBUG_KEY, next ? "1" : "0");
      } catch {
        /* private mode — keep in memory */
      }
      return next;
    });

  // §14: velocity-matched buffer-trace, bucketed so it refetches only on a real
  // pace change. Prefer the scheduler's authoritative velocity when we have it.
  const bucket = velocityBucket(bufferState?.velocityWps ?? velocity);
  const traceQuery = useQuery({
    queryKey: [...queryKeys.bufferTrace(sessionId ?? ""), bucket],
    enabled: Boolean(sessionId),
    staleTime: 30_000,
    queryFn: async (): Promise<BufferPoint[]> => {
      const v = bucketVelocity(bucket);
      const { data, error } = await api.GET("/api/eval/buffer-trace/{session_id}", {
        params: {
          path: { session_id: sessionId as string },
          query: v != null ? { velocity: v } : {},
        },
      });
      if (error || !data) throw new Error("buffer trace failed");
      return data;
    },
  });
  const trace = useMemo(() => traceQuery.data ?? [], [traceQuery.data]);
  const tMax = trace[trace.length - 1]?.t ?? 0;

  // The reader is "active" shortly after any focus move (for trace playback).
  const lastMoveRef = useRef(performance.now());
  useEffect(() => {
    lastMoveRef.current = performance.now();
  }, [focusWord]);

  // §4: rAF-driven trace playback (live occupancy takes over when video is real),
  // paused while the window is hidden (Page Visibility) or the reader is idle.
  const [tracedOccupancy, setTracedOccupancy] = useState(0);
  const [cursorT, setCursorT] = useState(0);
  const cursorRef = useRef(0);
  const liveAhead =
    bufferState && bufferState.committedAheadS > 0.05 ? bufferState.committedAheadS : null;
  useEffect(() => {
    if (liveAhead != null || tMax <= 0) return; // live occupancy drives it instead
    let raf = 0;
    let prev = performance.now();
    let stopped = false;
    const frame = (): void => {
      if (stopped) return;
      const now = performance.now();
      const dt = Math.min((now - prev) / 1000, 0.1);
      prev = now;
      if (!document.hidden && isReaderActive(lastMoveRef.current, now)) {
        cursorRef.current = advanceSawtoothCursor(cursorRef.current, dt, tMax);
        setCursorT(cursorRef.current);
        setTracedOccupancy(sampleSawtoothAt(trace, cursorRef.current));
      }
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    // Reset the dt baseline on resume so a long hide doesn't jump the cursor.
    const onVisible = (): void => {
      prev = performance.now();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      cancelAnimationFrame(raf);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [liveAhead, tMax, trace]);

  const high = bufferState?.high ?? trace[trace.length - 1]?.high ?? 75;
  const low = bufferState?.low ?? trace[trace.length - 1]?.low ?? 25;
  const commitHorizon = bufferState?.commitHorizon ?? 45;
  const displayed = liveAhead ?? tracedOccupancy;
  const frac = bufferFraction(displayed, high);
  const lowFrac = bufferFraction(low, high);

  // §6/§12: zone + stall, from the shared core classifier. `active` prefers the
  // backend idle flag (authoritative) and falls back to recent motion.
  const active = bufferState ? !bufferState.idle : isReaderActive(lastMoveRef.current, performance.now());
  const surface = classifyBufferSurface({
    authoritativeZone: bufferState?.zone ?? null,
    stage,
    budgetLow,
    fraction: frac,
    active,
    liveCommittedAheadS: bufferState ? bufferState.committedAheadS : null,
    inflightCommitted: bufferState?.inflightCommitted ?? 0,
  });

  // §3/§9: pulse on *real* promotions (a true burst) when live; on a rising traced
  // occupancy (a sim refill) otherwise.
  const prevOccRef = useRef(displayed);
  const rising = displayed > prevOccRef.current + 0.15;
  useEffect(() => {
    prevOccRef.current = displayed;
  });
  const pulsing = liveAhead != null ? (bufferState?.bursting ?? false) : rising;

  // §5: announce zone transitions to assistive tech (polite, only on change).
  const [announced, setAnnounced] = useState(surface.label);
  useEffect(() => {
    setAnnounced(surface.label);
  }, [surface.label]);

  const health = useMemo(() => bufferHealth(trace), [trace]);

  if (!sessionId) return null;

  const fillColor = surface.stalled ? "#fb7185" : budgetLow ? "#fbbf24" : "#f4a85d";
  const tone = surface.stalled ? "text-rose-300" : ZONE_TONE[surface.zone];
  const velocityWps = bufferState?.velocityWps ?? Math.abs(velocity);
  const inflight = bufferState?.inflightCommitted ?? 0;

  return (
    <div className="pointer-events-none absolute inset-x-0 top-0 z-20 select-none">
      <span className="sr-only" role="status" aria-live="polite">
        {`Film buffer: ${announced}`}
      </span>

      {/* The hairline — the faint generation indicator (§5.3). */}
      <div className="relative h-[2px] w-full bg-white/[0.06]">
        <span
          className="absolute top-[-1px] h-[4px] w-px bg-white/20"
          style={{ left: `${lowFrac * 100}%` }}
          aria-hidden
        />
        <div
          className="buffer-hairline-fill absolute inset-y-0 left-0"
          data-bursting={pulsing ? "true" : undefined}
          style={{
            width: `${frac * 100}%`,
            backgroundColor: fillColor,
            boxShadow: `0 0 6px ${fillColor}, 0 0 2px ${fillColor}`,
          }}
          role="progressbar"
          aria-label="Generated film buffered ahead"
          aria-valuenow={Math.round(displayed)}
          aria-valuemin={0}
          aria-valuemax={Math.round(high)}
        />
      </div>

      {/* Zone badge + degradation notice + debug toggle — left-aligned, clear of
          the crew-activity toggle (top-right). */}
      <div className="flex items-center gap-1.5 px-3 pt-1.5">
        <span
          className={`flex items-center gap-1 rounded-full bg-walnut-deep/55 px-2 py-0.5 text-[10px] font-medium backdrop-blur-sm transition-colors ${tone}`}
        >
          <span className="h-1 w-1 rounded-full bg-current" style={pulsing ? undefined : { opacity: 0.6 }} />
          {surface.label}
        </span>
        {inflight > 0 && !surface.stalled && (
          <span className="rounded-full bg-white/8 px-2 py-0.5 text-[10px] font-medium text-white/55 backdrop-blur-sm">
            Rendering {inflight} shot{inflight === 1 ? "" : "s"}
          </span>
        )}
        {budgetLow && (
          <span className="rounded-full bg-amber-400/15 px-2 py-0.5 text-[10px] font-medium text-amber-300 backdrop-blur-sm">
            {STAGE_NOTICE[stage]}
          </span>
        )}

        <button
          type="button"
          onClick={toggleDebug}
          aria-pressed={debug}
          aria-label="Buffer diagnostics"
          title="Buffer diagnostics"
          className="pointer-events-auto flex h-5 items-center gap-1 rounded-full bg-walnut-deep/50 px-1.5 text-[10px] text-white/45 backdrop-blur-sm transition hover:text-white/80 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ember-glow"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 12h4l3 8 4-16 3 8h4" />
          </svg>
        </button>
      </div>

      {/* Debug panel — the §13 proof: compact sawtooth, live readout, watermark
          legend, buffer-health verdict, zero video-seconds. */}
      {debug && (
        <div className="pointer-events-auto mx-3 mt-1.5 inline-flex flex-col gap-1.5 rounded-lg border border-white/10 bg-walnut-deep/80 p-2 backdrop-blur-md">
          <div className="flex items-center justify-between gap-4 text-[10px] tabular-nums text-white/55">
            <span className={tone}>{surface.label}</span>
            <span>
              {displayed.toFixed(0)}s / H {Math.round(high)}s · {velocityWps.toFixed(1)} wps
              {bufferState?.etaNextS != null ? ` · ETA ${bufferState.etaNextS.toFixed(1)}s` : ""}
              {pulsing ? " · burst" : ""}
            </span>
          </div>
          <MiniSawtooth trace={trace} cursorT={liveAhead != null ? tMax : cursorT} />
          <div className="flex items-center justify-between gap-3 text-[9px] tabular-nums text-white/40">
            <span>
              L {Math.round(low)}s · C {Math.round(commitHorizon)}s · H {Math.round(high)}s
            </span>
            {health.durationS > 0 && (
              <span>
                {(health.fractionAboveLow * 100).toFixed(0)}% above L · {health.stalls} stall
                {health.stalls === 1 ? "" : "s"}
              </span>
            )}
          </div>
          <p className="text-[9px] text-emerald-300/70">§4.10 buffer-trace · recomputed live · 0 video-seconds</p>
        </div>
      )}
    </div>
  );
}
