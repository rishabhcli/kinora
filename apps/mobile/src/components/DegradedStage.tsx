import { kenBurnsPreset, kenBurnsTempo, type SyncEngine } from "@kinora/core";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { AccessibilityInfo, Animated, Easing, Image, StyleSheet, Text, View } from "react-native";

import { useReducedMotion } from "../hooks/useReducedMotion";
import { alpha, palette, radius, space, type } from "../theme/tokens";

export type DegradedVariant = "keyframe" | "illustration" | "audio_text";

interface DegradedStageProps {
  /** The still to pan — a generated keyframe or the book's page image. */
  stillUrl: string | null;
  variant: DegradedVariant;
  /** Beat id — seeds the deterministic Ken-Burns move (stable across re-reads). */
  seed: string | null;
  budgetRemaining: number | null;
  underBudgetPressure: boolean;
  /**
   * The engine — optional, but when supplied the pan adapts to reading velocity
   * (§4.6), upcoming stills are warmed (§4.4), and a still whose URL fails drops
   * itself so the ladder steps down a rung (§12.4) instead of showing a gap.
   */
  engine?: SyncEngine;
  /** Freeze the pan while the room is idle / the app is backgrounded (§4.7). */
  paused?: boolean;
}

const LABEL: Record<DegradedVariant, string> = {
  keyframe: "Composing the next shot",
  illustration: "Reading ahead",
  audio_text: "Reading ahead",
};

const DOT: Record<DegradedVariant, string> = {
  keyframe: palette.emberGlow,
  illustration: "#8fc7e8",
  audio_text: "rgba(255,255,255,0.5)",
};

const ANNOUNCE: Record<DegradedVariant, string> = {
  keyframe: "Showing a preview still while the film renders.",
  illustration: "Showing the book's illustration while the film renders.",
  audio_text: "Narrated read-along; the film is rendering.",
};

/** Subscribe to the engine's reading velocity (0 when no engine is wired). */
function useEngineVelocity(engine?: SyncEngine): number {
  const subscribe = useCallback((cb: () => void) => engine?.subscribe(cb) ?? (() => {}), [engine]);
  const get = useCallback(() => engine?.getSnapshot().velocity ?? 0, [engine]);
  return useSyncExternalStore(subscribe, get);
}

/**
 * The §12.4 degradation ladder, mobile side — a slow native-driven `Animated`
 * Ken-Burns over the beat's keyframe (or the book's page image) at **zero
 * generation cost** (§4.4), chosen deterministically from the beat id so a
 * re-read replays the identical motion. It is **velocity-adaptive** (calms/slows
 * then freezes as the reader quickens, §4.6), **idle-paused** (rests when the app
 * backgrounds, §4.7), **self-healing** (a still that fails to load drops itself so
 * the ladder steps down rather than showing a gap), **decode-ahead** (warms the
 * next beats' stills), and **accessible** (announces the rung). Honors reduce-motion.
 */
export function DegradedStage({
  stillUrl,
  variant,
  seed,
  budgetRemaining,
  underBudgetPressure,
  engine,
  paused = false,
}: DegradedStageProps) {
  const reduced = useReducedMotion();
  const velocity = useEngineVelocity(engine);
  const tempo = kenBurnsTempo(velocity);
  const preset = kenBurnsPreset(seed);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const pan = useRef(new Animated.Value(0)).current;
  const pulse = useRef(new Animated.Value(0)).current;

  const frozen = paused || reduced || tempo.paused;

  // The Ken-Burns pan: a slow ping-pong, its duration eased by reading pace; it
  // rests entirely when frozen (idle / skim / reduce-motion).
  useEffect(() => {
    if (frozen || !stillUrl || size.w === 0) return;
    pan.setValue(0);
    const dur = preset.durationS * 1000 * tempo.durationScale;
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pan, { toValue: 1, duration: dur, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
        Animated.timing(pan, { toValue: 0, duration: dur, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [frozen, pan, preset, tempo.durationScale, stillUrl, size.w]);

  // The bottom-rung ember beacon (no still) — a breathing dot, never a spinner.
  useEffect(() => {
    if (frozen || stillUrl) return;
    pulse.setValue(0);
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, { toValue: 1, duration: 1400, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
        Animated.timing(pulse, { toValue: 0, duration: 1400, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [frozen, pulse, stillUrl]);

  // Decode-ahead: warm the next few beats' stills so arriving there is instant.
  useEffect(() => {
    if (!engine) return;
    for (const url of engine.upcomingStillUrls(3)) void Image.prefetch(url).catch(() => undefined);
  }, [engine, seed, stillUrl]);

  // Announce the rung to assistive tech when it changes.
  useEffect(() => {
    AccessibilityInfo.announceForAccessibility(ANNOUNCE[variant]);
  }, [variant]);

  const transform = frozen
    ? [{ scale: preset.fromScale }]
    : [
        { scale: pan.interpolate({ inputRange: [0, 1], outputRange: [preset.fromScale, preset.toScale] }) },
        { translateX: pan.interpolate({ inputRange: [0, 1], outputRange: [preset.fromX * size.w, preset.toX * size.w] }) },
        { translateY: pan.interpolate({ inputRange: [0, 1], outputRange: [preset.fromY * size.h, preset.toY * size.h] }) },
      ];

  return (
    <View
      style={styles.fill}
      onLayout={(e) => setSize({ w: e.nativeEvent.layout.width, h: e.nativeEvent.layout.height })}
      accessible
      accessibilityRole="image"
      accessibilityLabel={ANNOUNCE[variant]}
    >
      {stillUrl ? (
        <Animated.Image
          source={{ uri: stillUrl }}
          resizeMode="cover"
          style={[StyleSheet.absoluteFill, { transform }]}
          // A failed (expired/404) still drops itself so the ladder steps down.
          onError={() => engine?.dropCurrentStill()}
        />
      ) : (
        <View style={styles.floor}>
          <Animated.View
            style={[styles.beacon, !frozen && { opacity: pulse.interpolate({ inputRange: [0, 1], outputRange: [0.3, 0.85] }) }]}
          />
          <Text style={styles.floorText}>The narration carries you — the film catches up as you read.</Text>
        </View>
      )}

      {/* Cinematic letterbox so a still reads as a held establishing shot. */}
      <View style={[styles.bar, styles.barTop]} pointerEvents="none" />
      <View style={[styles.bar, styles.barBottom]} pointerEvents="none" />

      {/* Rung chip with a quality dot — a legible cue for the current rung. */}
      <View style={styles.caption} pointerEvents="none">
        <View style={[styles.dot, { backgroundColor: DOT[variant] }]} />
        <Text style={styles.captionText}>{LABEL[variant]}</Text>
      </View>
      {underBudgetPressure && (
        <View style={styles.budgetPill} pointerEvents="none">
          <Text style={styles.budgetText}>
            {budgetRemaining !== null
              ? `Saving film — ${Math.max(0, Math.round(budgetRemaining))}s left`
              : "Saving film budget"}
          </Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  fill: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, overflow: "hidden", backgroundColor: "#0b0705" },
  floor: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, alignItems: "center", justifyContent: "center", gap: space.md, paddingHorizontal: space.xl },
  beacon: { width: 44, height: 44, borderRadius: 22, backgroundColor: alpha.emberSoft, borderWidth: 1, borderColor: alpha.emberSoft },
  floorText: { color: alpha.white55, fontSize: type.caption.fontSize, textAlign: "center", maxWidth: 240 },
  bar: { position: "absolute", left: 0, right: 0, height: "7%", backgroundColor: "rgba(0,0,0,0.55)" },
  barTop: { top: 0 },
  barBottom: { bottom: 0 },
  caption: {
    position: "absolute",
    left: space.md,
    bottom: space.md,
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm,
    backgroundColor: "rgba(0,0,0,0.45)",
    borderRadius: radius.pill,
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 2,
  },
  dot: { width: 7, height: 7, borderRadius: 4 },
  captionText: { color: palette.parchment, fontSize: type.caption.fontSize, fontWeight: "500" },
  budgetPill: {
    position: "absolute",
    right: space.md,
    bottom: space.md,
    backgroundColor: "rgba(240,180,80,0.16)",
    borderRadius: radius.pill,
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 1,
  },
  budgetText: { color: "#f0d28a", fontSize: type.micro.fontSize, fontWeight: "500" },
});
