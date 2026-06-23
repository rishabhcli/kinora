import {
  type ArmPair,
  type ErrorEnvelope,
  type EvalReport,
  type MetricMeta,
  METRICS,
  barFraction,
  crewWins,
  improvementPct,
  isEvalReport,
  meetsThreshold,
  metricDomainMax,
  queryKeys,
  reportVerdict,
  summarizeReport,
} from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { GhostButton, Surface } from "../components/ui";
import { api } from "../lib/api";
import {
  alpha,
  BOTTOM_INSET,
  fonts,
  palette,
  radius,
  space,
  TOP_INSET,
  type,
} from "../theme/tokens";

/** A single arm's bar — track + fill, the winner inked ember. */
function Bar({ frac, ember }: { frac: number; ember: boolean }) {
  return (
    <View style={styles.barTrack}>
      <View
        style={[
          styles.barFill,
          { width: `${Math.max(frac * 100, 2)}%`, backgroundColor: ember ? palette.emberGlow : alpha.white40 },
        ]}
      />
    </View>
  );
}

/** One metric: crew vs baseline bars + improvement badge + the gate verdict. */
function MetricRow({ meta, pair, report }: { meta: MetricMeta; pair: ArmPair; report: EvalReport }) {
  const domain = metricDomainMax(meta, pair);
  const crewBetter = crewWins(meta, pair);
  const imp = improvementPct(meta, pair);
  const gate = meetsThreshold(meta, pair.crew, report.thresholds);
  return (
    <View style={styles.metric}>
      <View style={styles.metricHead}>
        <Text style={styles.metricLabel}>{meta.label}</Text>
        {imp !== null ? (
          <Text style={[styles.badge, crewBetter ? styles.badgeGood : styles.badgeBad]}>
            {crewBetter ? "▲" : "▼"} {Math.abs(imp).toFixed(0)}%
          </Text>
        ) : null}
      </View>
      <View style={styles.barLine}>
        <Text style={styles.armLabel}>Crew</Text>
        <Bar frac={barFraction(pair.crew, domain)} ember={crewBetter} />
        <Text style={styles.armValue}>{meta.format(pair.crew)}</Text>
      </View>
      <View style={styles.barLine}>
        <Text style={styles.armLabel}>Base</Text>
        <Bar frac={barFraction(pair.baseline, domain)} ember={!crewBetter} />
        <Text style={[styles.armValue, styles.armValueMuted]}>{meta.format(pair.baseline)}</Text>
      </View>
      <Text style={styles.gateNote}>
        {meta.higherIsBetter ? "Higher is better" : "Lower is better"}
        {gate !== null ? `  ·  gate ${gate ? "met ✓" : "missed ✗"}` : ""}
      </Text>
    </View>
  );
}

/** Per-character CCS, weakest-crew first, below-gate rows flagged. */
function PerCharacter({ report }: { report: EvalReport }) {
  const { crew, baseline } = report.per_character_ccs;
  const min = report.thresholds.ccs_min;
  const rows = Array.from(new Set([...Object.keys(crew), ...Object.keys(baseline)]))
    .map((key) => {
      const c = crew[key] ?? null;
      return { key, crew: c, baseline: baseline[key] ?? null, weak: c !== null && c < min };
    })
    .sort((a, b) => (a.crew ?? Infinity) - (b.crew ?? Infinity));
  if (rows.length === 0) return null;
  return (
    <View style={styles.section}>
      <Text style={styles.sectionLabel}>Per-character consistency</Text>
      {rows.map((r) => (
        <View key={r.key} style={[styles.charRow, r.weak ? styles.charRowWeak : null]}>
          <Text style={styles.charName} numberOfLines={1}>
            {r.key.replace(/^(character|char|entity)[_-]/i, "").replace(/[_-]+/g, " ")}
          </Text>
          <Text style={[styles.charScore, r.weak ? styles.charScoreWeak : null]}>
            {r.crew !== null ? r.crew.toFixed(3) : "—"}
          </Text>
          <Text style={styles.charBase}>{r.baseline !== null ? r.baseline.toFixed(3) : "—"}</Text>
        </View>
      ))}
    </View>
  );
}

type ReportResult =
  | { ok: true; report: EvalReport }
  | { ok: false; notReady: boolean; message: string };

/**
 * The §13 metrics proof on mobile — a bottom sheet showing the crew + shared
 * canon beating a single-agent, no-memory baseline (CCS, efficiency, regen rate,
 * style drift) with the headline verdict, per-character CCS, and a copy-friendly
 * summary. All the math is the shared, unit-tested `@kinora/core` eval helpers —
 * the same source of truth the desktop panel renders. (The live committed-buffer
 * sawtooth lives on the film's top hairline, §5.3.)
 */
export function MetricsSheet({
  visible,
  bookId,
  onClose,
}: {
  visible: boolean;
  bookId: string;
  onClose: () => void;
}) {
  const query = useQuery({
    queryKey: queryKeys.evalReport(bookId),
    enabled: visible,
    retry: false,
    staleTime: 0,
    queryFn: async (): Promise<ReportResult> => {
      const { data, error, response } = await api.GET("/api/eval/report/{book_id}", {
        params: { path: { book_id: bookId } },
      });
      if (data && isEvalReport(data)) return { ok: true, report: data };
      const message =
        (error as unknown as Partial<ErrorEnvelope> | undefined)?.error?.message ??
        "Could not load the eval report.";
      return { ok: false, notReady: response?.status === 404, message };
    },
  });

  const result = query.data;
  const report = result?.ok ? result.report : null;
  const verdict = report ? reportVerdict(report) : null;

  return (
    <Modal visible={visible} onRequestClose={onClose} transparent animationType="slide" statusBarTranslucent>
      <Pressable style={styles.scrim} onPress={onClose} accessibilityLabel="Close metrics" />
      <View pointerEvents="box-none" style={styles.dock}>
        <Surface style={styles.card}>
          <View style={styles.handle} />
          <ScrollView contentContainerStyle={styles.content} showsVerticalScrollIndicator={false} bounces={false}>
            <Text style={styles.eyebrow}>Proof · §13</Text>
            <Text style={styles.title}>Metrics</Text>
            <Text style={styles.lede}>
              Crew + shared canon vs a single agent with no memory — same book, seeds and prompts.
            </Text>

            {query.isLoading ? (
              <View style={styles.status}>
                <ActivityIndicator color={palette.emberGlow} />
              </View>
            ) : report && verdict ? (
              <>
                <View style={[styles.verdict, verdict.sweep ? styles.verdictStrong : styles.verdictPartial]}>
                  <Text style={styles.verdictHeadline}>{verdict.headline}</Text>
                  <Text style={styles.verdictNote}>
                    Thresholds were pre-registered (§9.5) before the run — they can’t be tuned to
                    flatter the result.
                  </Text>
                  <Text style={styles.verdictNums}>
                    CCS {report.ccs.crew.toFixed(3)} vs {report.ccs.baseline.toFixed(3)} · Efficiency{" "}
                    {report.efficiency.crew.toFixed(1)}% vs {report.efficiency.baseline.toFixed(1)}% ·{" "}
                    {report.runs} run{report.runs === 1 ? "" : "s"}
                  </Text>
                </View>

                <View style={styles.section}>
                  <Text style={styles.sectionLabel}>Crew vs single-agent baseline</Text>
                  {METRICS.map((meta) => (
                    <MetricRow key={meta.key} meta={meta} pair={report[meta.key]} report={report} />
                  ))}
                </View>

                <PerCharacter report={report} />

                <View style={styles.section}>
                  <Text style={styles.sectionLabel}>Demo summary (long-press to copy)</Text>
                  <Text selectable style={styles.summary}>
                    {summarizeReport(report, null)}
                  </Text>
                </View>
              </>
            ) : (
              <View style={styles.empty}>
                <Text style={styles.emptyTitle}>No eval report cached yet</Text>
                <Text style={styles.emptyMsg}>
                  {result && !result.ok ? result.message : "Run the eval CLI to produce the proof."}
                </Text>
                <Text selectable style={styles.command}>
                  python -m app.eval.run --book {bookId}
                </Text>
                <GhostButton label="Retry" onPress={() => void query.refetch()} />
              </View>
            )}

            <GhostButton label="Close" onPress={onClose} />
          </ScrollView>
        </Surface>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: { ...StyleSheet.absoluteFill, backgroundColor: "rgba(8,5,3,0.62)" },
  dock: { flex: 1, justifyContent: "flex-end" },
  card: {
    borderBottomLeftRadius: 0,
    borderBottomRightRadius: 0,
    paddingTop: space.md,
    paddingHorizontal: space.xxl,
    paddingBottom: BOTTOM_INSET + space.lg,
    maxHeight: "92%",
    marginTop: TOP_INSET,
  },
  handle: { alignSelf: "center", width: 40, height: 4, borderRadius: radius.pill, backgroundColor: alpha.white16, marginBottom: space.lg },
  content: { gap: space.lg, paddingBottom: space.lg },
  eyebrow: { color: palette.emberGlow, fontSize: type.micro.fontSize, letterSpacing: 1.6, textTransform: "uppercase" },
  title: { fontFamily: fonts.display, color: palette.parchment, fontSize: type.title.fontSize, lineHeight: type.title.lineHeight, fontWeight: "600", marginTop: 2 },
  lede: { color: alpha.white55, fontSize: type.caption.fontSize, lineHeight: type.label.lineHeight, marginTop: -space.sm },

  verdict: { borderRadius: radius.lg, borderWidth: StyleSheet.hairlineWidth, padding: space.md, gap: 6 },
  verdictStrong: { backgroundColor: "rgba(52,211,153,0.10)", borderColor: "rgba(52,211,153,0.35)" },
  verdictPartial: { backgroundColor: "rgba(251,191,36,0.10)", borderColor: "rgba(251,191,36,0.35)" },
  verdictHeadline: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "700" },
  verdictNote: { color: alpha.white55, fontSize: type.micro.fontSize, lineHeight: type.caption.lineHeight },
  verdictNums: { color: alpha.white72, fontSize: type.micro.fontSize, fontVariant: ["tabular-nums"] },

  section: { gap: space.sm },
  sectionLabel: { color: alpha.white55, fontSize: type.caption.fontSize, letterSpacing: 0.6, textTransform: "uppercase", marginLeft: 2 },

  metric: { gap: 5, paddingVertical: space.sm, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: alpha.white08 },
  metricHead: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  metricLabel: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "600" },
  badge: { fontSize: type.micro.fontSize, fontWeight: "700", fontVariant: ["tabular-nums"], overflow: "hidden", borderRadius: radius.pill, paddingHorizontal: space.sm, paddingVertical: 2 },
  badgeGood: { color: "#6ee7b7", backgroundColor: "rgba(52,211,153,0.14)" },
  badgeBad: { color: "#fda4af", backgroundColor: "rgba(251,113,133,0.14)" },
  barLine: { flexDirection: "row", alignItems: "center", gap: space.sm },
  armLabel: { width: 38, color: alpha.white40, fontSize: type.micro.fontSize },
  barTrack: { flex: 1, height: 8, borderRadius: radius.pill, backgroundColor: alpha.white08, overflow: "hidden" },
  barFill: { height: 8, borderRadius: radius.pill },
  armValue: { width: 62, textAlign: "right", color: palette.parchment, fontSize: type.caption.fontSize, fontWeight: "600", fontVariant: ["tabular-nums"] },
  armValueMuted: { color: alpha.white55, fontWeight: "400" },
  gateNote: { color: alpha.white40, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.4 },

  charRow: { flexDirection: "row", alignItems: "center", paddingVertical: 6, paddingHorizontal: space.sm, borderRadius: radius.sm },
  charRowWeak: { backgroundColor: "rgba(251,113,133,0.08)" },
  charName: { flex: 1, color: alpha.white85, fontSize: type.caption.fontSize, textTransform: "capitalize" },
  charScore: { width: 60, textAlign: "right", color: alpha.white85, fontSize: type.caption.fontSize, fontVariant: ["tabular-nums"] },
  charScoreWeak: { color: "#fda4af" },
  charBase: { width: 60, textAlign: "right", color: alpha.white40, fontSize: type.caption.fontSize, fontVariant: ["tabular-nums"] },

  summary: { color: alpha.white72, fontSize: type.micro.fontSize, lineHeight: type.caption.lineHeight, fontVariant: ["tabular-nums"], backgroundColor: "rgba(0,0,0,0.3)", borderRadius: radius.md, padding: space.md },

  empty: { alignItems: "center", gap: space.sm, paddingVertical: space.lg },
  emptyTitle: { color: palette.parchment, fontSize: type.heading.fontSize, fontWeight: "700" },
  emptyMsg: { color: alpha.white55, fontSize: type.caption.fontSize, textAlign: "center", lineHeight: type.label.lineHeight },
  command: { color: palette.emberGlow, fontSize: type.caption.fontSize, fontVariant: ["tabular-nums"], backgroundColor: "rgba(0,0,0,0.35)", borderRadius: radius.sm, paddingHorizontal: space.md, paddingVertical: space.sm, overflow: "hidden" },

  status: { paddingVertical: space.xxl, alignItems: "center" },
});
