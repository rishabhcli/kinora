import {
  type ConflictActivity,
  type ConflictOption,
  conflictOptionLabel,
  type ConflictTrace,
} from "@kinora/core";
import { useVideoPlayer, VideoView } from "expo-video";
import { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { alpha, BOTTOM_INSET, fonts, palette, radius, space, type } from "../theme/tokens";

interface ConflictSheetProps {
  /** The surfaced §7.2 conflict, or null when the crew is in agreement. */
  conflict: ConflictActivity | null;
  /** The streamed resolution (Showrunner reasoning + resolved flag). */
  trace: ConflictTrace;
  /** The disputed shot's current clip, shown (paused) as the "frame in question". */
  shotClipUrl: string | null;
  onResolve: (conflictId: string, option: string) => void;
  onDismiss: (conflictId: string) => void;
}

const OPTION_ORDER: Record<string, number> = { honor_canon: 0, evolve_canon: 1, surface_to_user: 2 };

/** A short cost/precondition caption for one option. */
function optionCaption(option: ConflictOption): string {
  if (option.requires) return `needs ${option.requires}`;
  if (option.cost_video_s && option.cost_video_s > 0) return `+${Math.round(option.cost_video_s)}s render`;
  if (option.cost_video_s === 0) return "no new render";
  return "";
}

/**
 * The mobile Crew-dispute sheet — the §7.2 "money shot" on phones. When the
 * Continuity Supervisor flags a canon violation the Showrunner can't auto-resolve,
 * it slides up here: the claim, the canon fact it contradicts, and the policy
 * options. The reader picks; the Showrunner's arbitration then streams in and the
 * affected shot regenerates (or canon evolves) per the choice.
 */
export function ConflictSheet({
  conflict,
  trace,
  shotClipUrl,
  onResolve,
  onDismiss,
}: ConflictSheetProps) {
  const [picked, setPicked] = useState<string | null>(null);

  const conflictId = conflict?.conflictId ?? null;
  useEffect(() => {
    setPicked(null);
  }, [conflictId]);

  // The disputed shot's frame: load the clip into a paused player so the
  // VideoView shows a still (no playback, no controls).
  const framePlayer = useVideoPlayer(null);
  useEffect(() => {
    if (!shotClipUrl) return;
    void framePlayer
      .replaceAsync(shotClipUrl)
      .then(() => framePlayer.pause())
      .catch(() => undefined);
  }, [shotClipUrl, framePlayer]);

  const chosen = picked ?? trace.chosen;
  const phase: "options" | "arbitrating" | "resolved" = trace.resolved
    ? "resolved"
    : chosen
      ? "arbitrating"
      : "options";

  const options = conflict
    ? [...conflict.options].sort((a, b) => (OPTION_ORDER[a.id] ?? 9) - (OPTION_ORDER[b.id] ?? 9))
    : [];

  const choose = (option: string): void => {
    if (!conflict) return;
    setPicked(option);
    onResolve(conflict.conflictId, option);
  };

  const close = (): void => {
    if (conflict) onDismiss(conflict.conflictId);
  };

  return (
    <Modal
      visible={conflict != null}
      transparent
      animationType="slide"
      onRequestClose={close}
      statusBarTranslucent
    >
      <View style={styles.root}>
        <Pressable style={styles.backdrop} onPress={close} accessibilityLabel="Dismiss dispute" />

        <View style={styles.sheet}>
          <View style={styles.grabber} />
          <View style={styles.accent} />

          {conflict && (
            <ScrollView contentContainerStyle={styles.body} showsVerticalScrollIndicator={false}>
              <View style={styles.headerRow}>
                <Text style={styles.eyebrow}>● Crew dispute · §7.2</Text>
                <Pressable onPress={close} hitSlop={12} accessibilityLabel="Close">
                  <Text style={styles.close}>✕</Text>
                </Pressable>
              </View>
              <Text style={styles.title}>
                {conflict.raisedBy?.includes("continuity")
                  ? "Continuity flagged a canon violation"
                  : "A canon violation needs you"}
              </Text>

              <View style={styles.frame}>
                {shotClipUrl ? (
                  <VideoView
                    style={styles.frameVideo}
                    player={framePlayer}
                    nativeControls={false}
                    contentFit="cover"
                  />
                ) : (
                  <View style={styles.framePlaceholder} />
                )}
                <View style={styles.frameTag}>
                  <Text style={styles.frameTagText}>in question</Text>
                </View>
              </View>

              <View style={styles.factCard}>
                <Text style={styles.factLabel}>The shot depicts</Text>
                <Text style={styles.factText}>
                  {conflict.claim ?? "a contradiction with the established canon"}
                </Text>
                {conflict.canonFact ? (
                  <>
                    <Text style={[styles.factLabel, styles.factLabelSpaced]}>But canon says</Text>
                    <Text style={styles.canonText}>{conflict.canonFact}</Text>
                  </>
                ) : null}
              </View>

              {phase === "options" ? (
                <>
                  <Text style={styles.prompt}>How should the crew resolve it?</Text>
                  {options.map((option) => {
                    const primary = option.id === "honor_canon";
                    const caption = optionCaption(option);
                    return (
                      <Pressable
                        key={option.id}
                        onPress={() => choose(option.id)}
                        accessibilityRole="button"
                        style={({ pressed }) => [
                          styles.option,
                          primary ? styles.optionPrimary : styles.optionPlain,
                          pressed && styles.optionPressed,
                        ]}
                      >
                        <View style={styles.optionTextWrap}>
                          <Text style={styles.optionTitle}>{conflictOptionLabel(option.id)}</Text>
                          <Text style={styles.optionAction} numberOfLines={1}>
                            {option.action}
                          </Text>
                        </View>
                        {caption ? <Text style={styles.optionCaption}>{caption}</Text> : null}
                      </Pressable>
                    );
                  })}
                </>
              ) : (
                <View style={styles.resolutionCard}>
                  <View style={styles.resolutionHead}>
                    {phase === "resolved" ? (
                      <Text style={styles.resolvedTick}>✓</Text>
                    ) : (
                      <ActivityIndicator size="small" color={palette.emberGlow} />
                    )}
                    <Text style={styles.resolutionTitle}>
                      {phase === "resolved"
                        ? `Resolved — ${conflictOptionLabel(chosen)}`
                        : `Showrunner is arbitrating — ${conflictOptionLabel(chosen)}`}
                    </Text>
                  </View>

                  {trace.reasoning.length === 0 ? (
                    <Text style={styles.reasoningPending}>Consulting the canon graph…</Text>
                  ) : (
                    trace.reasoning.map((line, i) => (
                      <Text key={i} style={styles.reasoningLine}>
                        {line}
                      </Text>
                    ))
                  )}

                  {phase === "resolved" ? (
                    <Pressable onPress={close} style={styles.doneBtn} accessibilityRole="button">
                      <Text style={styles.doneLabel}>Back to reading</Text>
                    </Pressable>
                  ) : null}
                </View>
              )}
            </ScrollView>
          )}
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, justifyContent: "flex-end" },
  backdrop: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: {
    backgroundColor: palette.walnutWall,
    borderTopLeftRadius: radius.glass,
    borderTopRightRadius: radius.glass,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white16,
    overflow: "hidden",
    maxHeight: "88%",
  },
  grabber: {
    alignSelf: "center",
    width: 40,
    height: 4,
    borderRadius: 2,
    backgroundColor: alpha.white16,
    marginTop: space.sm,
  },
  accent: { height: 3, backgroundColor: palette.danger, marginTop: space.sm },
  body: { padding: space.xl, paddingBottom: BOTTOM_INSET + space.lg, gap: space.md },

  headerRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  eyebrow: {
    color: palette.danger,
    fontSize: type.micro.fontSize,
    fontWeight: "700",
    letterSpacing: 1,
    textTransform: "uppercase",
  },
  close: { color: alpha.white55, fontSize: 18, fontWeight: "600" },
  title: { color: palette.parchment, fontFamily: fonts.display, fontSize: type.title.fontSize, lineHeight: type.title.lineHeight },

  frame: {
    width: "100%",
    aspectRatio: 16 / 9,
    borderRadius: radius.md,
    overflow: "hidden",
    backgroundColor: "#000",
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white12,
  },
  frameVideo: { width: "100%", height: "100%" },
  framePlaceholder: {
    width: "100%",
    height: "100%",
    backgroundColor: "rgba(36,24,18,0.9)",
  },
  frameTag: {
    position: "absolute",
    bottom: space.xs,
    left: space.xs,
    backgroundColor: "rgba(0,0,0,0.65)",
    borderRadius: radius.sm,
    paddingHorizontal: space.sm,
    paddingVertical: 2,
  },
  frameTagText: {
    color: palette.danger,
    fontSize: type.micro.fontSize,
    fontWeight: "700",
    textTransform: "uppercase",
    letterSpacing: 0.5,
  },

  factCard: {
    backgroundColor: alpha.glassFillSoft,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white12,
    padding: space.md,
    gap: space.xs,
  },
  factLabel: {
    color: alpha.white40,
    fontSize: type.micro.fontSize,
    fontWeight: "700",
    textTransform: "uppercase",
    letterSpacing: 0.5,
  },
  factLabelSpaced: { marginTop: space.sm },
  factText: { color: palette.parchment, fontSize: type.body.fontSize, lineHeight: 22 },
  canonText: { color: palette.emberGlow, fontSize: type.label.fontSize, lineHeight: 20 },

  prompt: { color: alpha.white55, fontSize: type.label.fontSize, marginTop: space.xs },
  option: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.md,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    paddingVertical: space.md,
    paddingHorizontal: space.lg,
  },
  optionPrimary: { backgroundColor: alpha.emberSoft, borderColor: "rgba(224,134,58,0.4)" },
  optionPlain: { backgroundColor: alpha.white08, borderColor: alpha.white12 },
  optionPressed: { opacity: 0.7 },
  optionTextWrap: { flex: 1, gap: 2 },
  optionTitle: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "700", textTransform: "capitalize" },
  optionAction: { color: alpha.white55, fontSize: type.caption.fontSize },
  optionCaption: {
    color: alpha.white72,
    fontSize: type.micro.fontSize,
    fontWeight: "600",
    backgroundColor: "rgba(0,0,0,0.3)",
    paddingHorizontal: space.sm,
    paddingVertical: 3,
    borderRadius: radius.pill,
    overflow: "hidden",
  },

  resolutionCard: {
    backgroundColor: "rgba(0,0,0,0.22)",
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white12,
    padding: space.md,
    gap: space.sm,
  },
  resolutionHead: { flexDirection: "row", alignItems: "center", gap: space.sm },
  resolvedTick: { color: "#7fd1a6", fontSize: 16, fontWeight: "800" },
  resolutionTitle: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "700", flex: 1 },
  reasoningPending: { color: alpha.white40, fontSize: type.caption.fontSize, fontStyle: "italic" },
  reasoningLine: {
    color: alpha.white72,
    fontSize: type.caption.fontSize,
    lineHeight: 18,
    borderLeftWidth: 2,
    borderLeftColor: alpha.white12,
    paddingLeft: space.sm,
  },
  doneBtn: {
    marginTop: space.sm,
    backgroundColor: alpha.white95,
    borderRadius: radius.pill,
    paddingVertical: space.sm,
    alignItems: "center",
  },
  doneLabel: { color: palette.walnutDeep, fontSize: type.label.fontSize, fontWeight: "700" },
});
