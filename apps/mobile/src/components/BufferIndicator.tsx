import {
  type BeatStage,
  type BufferPoint,
  advanceSawtoothCursor,
  bufferFraction,
  classifyBufferSurface,
  isReaderActive,
  queryKeys,
  sampleSawtoothAt,
} from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Animated, Easing, Pressable, StyleSheet, Text, View } from "react-native";

import { useReducedMotion } from "../hooks/useReducedMotion";
import { api } from "../lib/api";
import { alpha, palette, radius, space, type } from "../theme/tokens";

/**
 * The §5.3 buffer indicator, mobile side — a faint hairline along the top of the
 * film that fills toward `H` (breathing during a refill burst, resting in the
 * `[L, H)` idle band) + a zone badge naming what's ahead, flipping to "Catching
 * up" if the reader outruns the render (§4.11) and warming amber under budget
 * pressure. Occupancy uses the live committed buffer when video is real, else it
 * plays the recomputed §4.10 buffer-trace sawtooth (velocity-matched, zero
 * video-seconds). Tap the badge for a compact diagnostics readout. All buffer
 * math is the shared, tested `@kinora/core` `sync/buffer`.
 */
interface BufferIndicatorProps {
  sessionId: string | null;
  focusWord: number;
  velocity: number;
  /** Live committed-seconds-ahead (snapshot.committedSecondsAhead); 0 with the gate off. */
  committedAheadS: number;
  stage: BeatStage;
  budgetLow: boolean;
}

const ACTIVE_WINDOW_MS = 2200;

const ZONE_TINT: Record<string, string> = {
  committed: palette.emberGlow,
  speculative: "#fbbf24",
  cold: alpha.white55,
};

type VelocityBucket = "slow" | "normal" | "fast";
function velocityBucket(v: number): VelocityBucket {
  const a = Math.abs(v);
  return a < 3 ? "slow" : a > 8 ? "fast" : "normal";
}
function bucketVelocity(bucket: VelocityBucket): number | undefined {
  return bucket === "slow" ? 2.5 : bucket === "fast" ? 10 : undefined;
}

/** A compact bar-sawtooth of the §4.10 trace (no SVG dependency on mobile). */
function BarSawtooth({ trace, cursorT, color }: { trace: BufferPoint[]; cursorT: number; color: string }) {
  const last = trace[trace.length - 1];
  if (trace.length < 2 || !last) return <View style={styles.barsEmpty} />;
  const tMax = Math.max(last.t, 1);
  const high = Math.max(...trace.map((p) => p.high), 1);
  const BARS = 26;
  const bars = Array.from({ length: BARS }, (_, i) => {
    const t = (i / (BARS - 1)) * tMax;
    return sampleSawtoothAt(trace, t) / high;
  });
  const cursorIdx = Math.round((Math.max(0, Math.min(cursorT, tMax)) / tMax) * (BARS - 1));
  return (
    <View style={styles.bars}>
      {bars.map((h, i) => (
        <View
          key={i}
          style={{
            flex: 1,
            marginHorizontal: 0.5,
            height: `${Math.max(4, h * 100)}%`,
            backgroundColor: i === cursorIdx ? "#fff" : color,
            opacity: i === cursorIdx ? 0.95 : 0.55,
            borderRadius: 1,
          }}
        />
      ))}
    </View>
  );
}

export function BufferIndicator({
  sessionId,
  focusWord,
  velocity,
  committedAheadS,
  stage,
  budgetLow,
}: BufferIndicatorProps) {
  const reduced = useReducedMotion();
  const [debug, setDebug] = useState(false);

  // §14: velocity-matched buffer-trace, bucketed so it refetches only on a real
  // change of pace.
  const bucket = velocityBucket(velocity);
  const traceQuery = useQuery({
    queryKey: [...queryKeys.bufferTrace(sessionId ?? ""), bucket],
    enabled: Boolean(sessionId),
    staleTime: 30_000,
    queryFn: async (): Promise<BufferPoint[]> => {
      const v = bucketVelocity(bucket);
      const { data, error } = await api.GET("/api/eval/buffer-trace/{session_id}", {
        params: { path: { session_id: sessionId as string }, query: v != null ? { velocity: v } : {} },
      });
      if (error || !data) throw new Error("buffer trace failed");
      return data;
    },
  });
  const trace = useMemo(() => traceQuery.data ?? [], [traceQuery.data]);
  const tMax = trace[trace.length - 1]?.t ?? 0;
  const high = trace[trace.length - 1]?.high ?? 75;
  const low = trace[trace.length - 1]?.low ?? 25;

  const lastMoveRef = useRef(Date.now());
  useEffect(() => {
    lastMoveRef.current = Date.now();
  }, [focusWord]);

  // Trace-playback cursor; advances while reading, holds while idle.
  const liveAhead = committedAheadS > 0.05 ? committedAheadS : null;
  const [tracedOccupancy, setTracedOccupancy] = useState(0);
  const [cursorT, setCursorT] = useState(0);
  const [rising, setRising] = useState(false);
  const cursorRef = useRef(0);
  const prevOccRef = useRef(0);
  useEffect(() => {
    if (liveAhead != null || tMax <= 0) return;
    let prev = Date.now();
    const id = setInterval(() => {
      const now = Date.now();
      const dt = (now - prev) / 1000;
      prev = now;
      if (isReaderActive(lastMoveRef.current, now, ACTIVE_WINDOW_MS)) {
        cursorRef.current = advanceSawtoothCursor(cursorRef.current, dt, tMax);
        const occ = sampleSawtoothAt(trace, cursorRef.current);
        setRising(occ > prevOccRef.current + 0.15);
        prevOccRef.current = occ;
        setCursorT(cursorRef.current);
        setTracedOccupancy(occ);
      } else {
        setRising(false);
      }
    }, 120);
    return () => clearInterval(id);
  }, [liveAhead, tMax, trace]);

  const displayed = liveAhead ?? tracedOccupancy;
  const frac = bufferFraction(displayed, high);
  const lowFrac = bufferFraction(low, high);
  const active = isReaderActive(lastMoveRef.current, Date.now(), ACTIVE_WINDOW_MS);
  const surface = classifyBufferSurface({
    stage,
    budgetLow,
    fraction: frac,
    active,
    // Mobile consumes buffer_state for the ladder only (no inflight), so a stall
    // never false-fires here — keyframe-by-design is not a stall.
    liveCommittedAheadS: liveAhead,
    inflightCommitted: 0,
  });
  const pulsing = rising && !reduced;

  // Breathe the fill during a refill burst.
  const pulse = useRef(new Animated.Value(1)).current;
  useEffect(() => {
    if (!pulsing) {
      pulse.setValue(1);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, { toValue: 0.5, duration: 700, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
        Animated.timing(pulse, { toValue: 1, duration: 700, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [pulse, pulsing]);

  if (!sessionId) return null;

  const fillColor = surface.stalled ? "#fb7185" : budgetLow ? "#fbbf24" : palette.emberGlow;
  const badgeColor = surface.stalled ? "#fda4af" : (ZONE_TINT[surface.zone] ?? palette.emberGlow);

  return (
    <View style={styles.root} pointerEvents="box-none">
      {/* The hairline — faint generation indicator along the top edge (§5.3). */}
      <View style={styles.track}>
        <View style={[styles.notch, { left: `${lowFrac * 100}%` }]} />
        <Animated.View
          style={[styles.fill, { width: `${frac * 100}%`, backgroundColor: fillColor, opacity: pulse }]}
        />
      </View>

      {/* Zone badge — tap for diagnostics (the only interactive bit). */}
      <Pressable
        onPress={() => setDebug((v) => !v)}
        accessibilityRole="button"
        accessibilityLabel="Buffer diagnostics"
        style={styles.badge}
      >
        <View style={[styles.badgeDot, { backgroundColor: fillColor, opacity: rising ? 1 : 0.6 }]} />
        <Text style={[styles.badgeText, { color: badgeColor }]}>{surface.label}</Text>
      </Pressable>

      {/* Compact diagnostics (the §13 proof, mobile) — bar sawtooth + readout. */}
      {debug && (
        <View style={styles.debug} pointerEvents="none">
          <BarSawtooth trace={trace} cursorT={liveAhead != null ? tMax : cursorT} color={palette.emberGlow} />
          <Text style={styles.debugText}>
            {displayed.toFixed(0)}s / H {Math.round(high)}s · {Math.abs(velocity).toFixed(1)} wps
          </Text>
          <Text style={styles.debugProof}>§4.10 buffer-trace · 0 video-seconds</Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { position: "absolute", top: 0, left: 0, right: 0 },
  track: { height: 2, width: "100%", backgroundColor: "rgba(255,255,255,0.06)" },
  notch: { position: "absolute", top: -1, width: 1, height: 4, backgroundColor: "rgba(255,255,255,0.22)" },
  fill: { height: 2 },
  badge: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    alignSelf: "flex-start",
    marginTop: space.xs + 2,
    marginLeft: space.md,
    backgroundColor: "rgba(0,0,0,0.42)",
    borderRadius: radius.pill,
    paddingHorizontal: space.sm,
    paddingVertical: 2,
  },
  badgeDot: { width: 5, height: 5, borderRadius: 3 },
  badgeText: { fontSize: type.micro.fontSize, fontWeight: "600" },
  debug: {
    alignSelf: "flex-start",
    marginTop: space.xs,
    marginLeft: space.md,
    width: 200,
    gap: 4,
    padding: space.sm,
    borderRadius: radius.md,
    backgroundColor: "rgba(22,14,8,0.8)",
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white12,
  },
  bars: { flexDirection: "row", alignItems: "flex-end", height: 34, width: "100%" },
  barsEmpty: { height: 34, width: "100%", borderRadius: 4, backgroundColor: "rgba(255,255,255,0.03)" },
  debugText: { color: alpha.white55, fontSize: type.micro.fontSize, fontVariant: ["tabular-nums"] },
  debugProof: { color: "rgba(110,231,183,0.7)", fontSize: 9 },
});
