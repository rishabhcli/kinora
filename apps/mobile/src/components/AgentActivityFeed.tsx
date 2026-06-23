import {
  type ActivityKind,
  activitySummary,
  type AgentActivity,
  type AgentRole,
  agentRoleLabel,
  type ConflictActivity,
  type FeedSummary,
  formatActivityLog,
  groupActivity,
  type RegenActivity,
  type SessionActivity,
  type ShotGroup,
  type SocketStatus,
  shortShotId,
  summarizeFeed,
  summarizeQa,
} from "@kinora/core";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Animated,
  Easing,
  Modal,
  Pressable,
  ScrollView,
  Share,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { useReducedMotion } from "../hooks/useReducedMotion";
import { alpha, BOTTOM_INSET, fonts, palette, radius, space, type } from "../theme/tokens";

interface AgentActivityFeedProps {
  /** The §5.4 feed, newest first. */
  activity: SessionActivity[];
  socketStatus: SocketStatus;
  visible: boolean;
  onClose: () => void;
  /** Jump the playhead to a shot (regen entry → its place in the film). */
  onSelectShot?: (shotId: string) => void;
  /** Re-open the conflict sheet for a surfaced dispute (§7.2). */
  onResolveConflict?: (conflict: ConflictActivity) => void;
}

type FilterKind = ActivityKind | "all";

const FILTERS: { id: FilterKind; label: string }[] = [
  { id: "all", label: "All" },
  { id: "agent", label: "Crew" },
  { id: "regen", label: "Renders" },
  { id: "conflict", label: "Conflicts" },
  { id: "scene", label: "Scenes" },
  { id: "budget", label: "Budget" },
];

/** Per-role monogram + accent (the six-member crew, §5.5). No SVG dep on mobile,
 *  so a colored monogram disc stands in for the desktop avatar icon. */
const ROLE_META: Record<AgentRole, { code: string; color: string }> = {
  showrunner: { code: "SR", color: palette.emberGlow },
  adapter: { code: "AD", color: "#c4b5fd" },
  continuity: { code: "CT", color: "#86efac" },
  cinematographer: { code: "CN", color: "#7dd3fc" },
  generator: { code: "GN", color: "#f0abfc" },
  critic: { code: "CR", color: "#fcd34d" },
  unknown: { code: "··", color: alpha.white55 },
};

const KIND_COLOR: Record<ActivityKind, string> = {
  agent: palette.emberGlow,
  regen: "#7dd3fc",
  budget: "#fcd34d",
  conflict: palette.danger,
  scene: "#86efac",
};

const LINK_META: Record<SocketStatus, { label: string; color: string; live: boolean }> = {
  open: { label: "Live", color: "#86efac", live: true },
  connecting: { label: "Reconnecting", color: "#fcd34d", live: true },
  closed: { label: "Offline", color: alpha.white40, live: false },
};

function relativeTime(at: number, now: number): string {
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 5) return "now";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h`;
}

/** A pulsing link dot (steady ring when reduce-motion is on). */
function LiveDot({ status }: { status: SocketStatus }) {
  const meta = LINK_META[status];
  const reduced = useReducedMotion();
  const pulse = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    if (!meta.live || reduced) return;
    const loop = Animated.loop(
      Animated.timing(pulse, {
        toValue: 1,
        duration: 1800,
        easing: Easing.out(Easing.ease),
        useNativeDriver: true,
      }),
    );
    loop.start();
    return () => loop.stop();
  }, [meta.live, reduced, pulse]);

  return (
    <View style={styles.dotWrap}>
      {meta.live && !reduced && (
        <Animated.View
          style={[
            styles.dotPulse,
            {
              backgroundColor: meta.color,
              opacity: pulse.interpolate({ inputRange: [0, 1], outputRange: [0.5, 0] }),
              transform: [{ scale: pulse.interpolate({ inputRange: [0, 1], outputRange: [1, 2.4] }) }],
            },
          ]}
        />
      )}
      <View style={[styles.dotCore, { backgroundColor: meta.color }]} />
    </View>
  );
}

/** The §13 efficiency strip — what the crew produced, distilled from the feed. */
function SummaryBar({ summary }: { summary: FeedSummary }) {
  const pass = summary.qaPass + summary.qaFail;
  const rate = pass > 0 ? Math.round((summary.qaPass / pass) * 100) : null;
  const stats: { label: string; value: string }[] = [{ label: "rendered", value: String(summary.renders) }];
  if (rate !== null) stats.push({ label: "QA pass", value: `${rate}%` });
  if (summary.avgCcs !== null) stats.push({ label: "avg CCS", value: summary.avgCcs.toFixed(2) });
  if (summary.conflictsRaised > 0) {
    stats.push({ label: "conflicts", value: `${summary.conflictsResolved}/${summary.conflictsRaised}` });
  }
  if (summary.scenesStitched > 0) stats.push({ label: "scenes", value: String(summary.scenesStitched) });
  return (
    <View style={styles.summary}>
      {stats.map((s) => (
        <View key={s.label} style={styles.stat}>
          <Text style={styles.statValue}>{s.value}</Text>
          <Text style={styles.statLabel}>{s.label}</Text>
        </View>
      ))}
    </View>
  );
}

function AgentEntry({ item }: { item: AgentActivity }) {
  const meta = ROLE_META[item.role];
  return (
    <View style={styles.row}>
      <View style={[styles.avatar, { borderColor: meta.color }]}>
        <Text style={[styles.avatarText, { color: meta.color }]}>{meta.code}</Text>
      </View>
      <View style={styles.rowBody}>
        <View style={styles.rowHead}>
          <Text style={styles.roleLabel}>{agentRoleLabel(item.role)}</Text>
          {item.aspect ? (
            <View style={styles.aspectTag}>
              <Text style={styles.aspectText}>{item.aspect}</Text>
            </View>
          ) : null}
        </View>
        <Text style={styles.message}>{item.message}</Text>
      </View>
    </View>
  );
}

function RegenEntry({ item, onSelectShot }: { item: RegenActivity; onSelectShot?: (shotId: string) => void }) {
  const qa = summarizeQa(item.qa);
  return (
    <Pressable onPress={() => onSelectShot?.(item.shotId)} disabled={!onSelectShot} accessibilityRole="button" style={styles.row}>
      <View style={[styles.kindDot, { backgroundColor: KIND_COLOR.regen }]} />
      <View style={styles.rowBody}>
        <Text style={styles.rowTitle}>
          Shot re-rendered <Text style={styles.shotId}>· {shortShotId(item.shotId)}</Text>
        </Text>
        {qa ? (
          <View style={styles.badgeRow}>
            {qa.passed !== null ? (
              <View
                style={[
                  styles.qaBadge,
                  { backgroundColor: qa.passed ? "rgba(134,239,172,0.16)" : "rgba(240,164,138,0.16)" },
                ]}
              >
                <Text style={[styles.qaText, { color: qa.passed ? "#86efac" : palette.danger }]}>
                  {qa.passed ? "QA pass" : "QA fail"}
                </Text>
              </View>
            ) : null}
            {qa.ccs !== null ? (
              <View style={styles.ccsBadge}>
                <Text style={styles.ccsText}>CCS {qa.ccs.toFixed(2)}</Text>
              </View>
            ) : null}
          </View>
        ) : null}
      </View>
    </Pressable>
  );
}

function ConflictEntry({
  item,
  onResolveConflict,
}: {
  item: ConflictActivity;
  onResolveConflict?: (conflict: ConflictActivity) => void;
}) {
  return (
    <View style={styles.conflictCard}>
      <Text style={styles.conflictTitle}>Continuity conflict</Text>
      {item.claim ? <Text style={styles.message}>{item.claim}</Text> : null}
      {item.canonFact ? <Text style={styles.canonText}>Canon: {item.canonFact}</Text> : null}
      {onResolveConflict ? (
        <Pressable onPress={() => onResolveConflict(item)} accessibilityRole="button" style={styles.resolveBtn}>
          <Text style={styles.resolveText}>Resolve…</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

function SimpleEntry({ kind, label, detail }: { kind: ActivityKind; label: string; detail?: string }) {
  return (
    <View style={styles.row}>
      <View style={[styles.kindDot, { backgroundColor: KIND_COLOR[kind] }]} />
      <View style={styles.rowBody}>
        <Text style={styles.message}>
          <Text style={styles.rowTitle}>{label}</Text>
          {detail ? <Text style={styles.detail}> — {detail}</Text> : null}
        </Text>
      </View>
    </View>
  );
}

/** Render one activity (used by single rows and inside a shot group). */
function Entry({
  item,
  onSelectShot,
  onResolveConflict,
}: {
  item: SessionActivity;
  onSelectShot?: (shotId: string) => void;
  onResolveConflict?: (conflict: ConflictActivity) => void;
}) {
  if (item.kind === "agent") return <AgentEntry item={item} />;
  if (item.kind === "regen") return <RegenEntry item={item} onSelectShot={onSelectShot} />;
  if (item.kind === "conflict") return <ConflictEntry item={item} onResolveConflict={onResolveConflict} />;
  if (item.kind === "budget") {
    return <SimpleEntry kind="budget" label="Budget low" detail={`${Math.round(item.remainingS)}s of film left`} />;
  }
  if (item.kind === "scene") return <SimpleEntry kind="scene" label="Scene stitched" detail={shortShotId(item.sceneId)} />;
  return null;
}

/** A collapsed shot lifecycle: the newest step + a toggle to reveal the rest. */
function ShotGroupRow({
  group,
  now,
  onSelectShot,
}: {
  group: ShotGroup;
  now: number;
  onSelectShot?: (shotId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [head, ...rest] = group.activities;
  if (!head) return null;
  return (
    <View style={styles.group}>
      <View style={styles.entry}>
        <View style={styles.entryMain}>
          <Entry item={head} onSelectShot={onSelectShot} />
        </View>
        <Text style={styles.time}>{relativeTime(head.at, now)}</Text>
      </View>
      <Pressable
        onPress={() => setExpanded((v) => !v)}
        accessibilityRole="button"
        style={styles.groupToggle}
      >
        <Text style={styles.groupToggleText}>
          {expanded
            ? "Hide steps"
            : `${rest.length} earlier ${rest.length === 1 ? "step" : "steps"} on shot ${shortShotId(group.shotId)}`}
        </Text>
      </Pressable>
      {expanded
        ? rest.map((a) => (
            <View key={a.id} style={[styles.entry, styles.groupChild]}>
              <View style={styles.entryMain}>
                <Entry item={a} onSelectShot={onSelectShot} />
              </View>
              <Text style={styles.time}>{relativeTime(a.at, now)}</Text>
            </View>
          ))
        : null}
    </View>
  );
}

/**
 * The §5.4 live agent-activity feed as a bottom sheet: the crew planning,
 * rendering + QA, arbitrating, and stitching — in real time, so a judge can watch
 * the multi-agent negotiation on a phone without backend logs. Per-shot crew
 * steps collapse into groups; a summary strip rolls up the §13 efficiency
 * numbers; the log is filterable and shareable; reduce-motion aware; and the
 * stream is a polite live region for screen readers.
 */
export function AgentActivityFeed({
  activity,
  socketStatus,
  visible,
  onClose,
  onSelectShot,
  onResolveConflict,
}: AgentActivityFeedProps) {
  const reduced = useReducedMotion();
  const [filter, setFilter] = useState<FilterKind>("all");
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!visible) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(t);
  }, [visible]);

  const link = LINK_META[socketStatus];
  const summary = useMemo(() => summarizeFeed(activity), [activity]);
  const grouped = useMemo(
    () => groupActivity(filter === "all" ? activity : activity.filter((a) => a.kind === filter)),
    [activity, filter],
  );

  function shareLog(): void {
    void Share.share({ message: formatActivityLog(activity) }).catch(() => undefined);
  }

  return (
    <Modal visible={visible} transparent animationType={reduced ? "none" : "slide"} onRequestClose={onClose}>
      <Pressable style={styles.backdrop} onPress={onClose} accessibilityLabel="Dismiss feed" />
      <View style={styles.sheet}>
        <View style={styles.grip} />
        <View style={styles.header}>
          <Text style={styles.heading}>Crew activity</Text>
          <View style={styles.link}>
            <LiveDot status={socketStatus} />
            <Text style={[styles.linkLabel, { color: link.color }]}>{link.label}</Text>
          </View>
          <Pressable onPress={shareLog} hitSlop={10} accessibilityRole="button" accessibilityLabel="Export log" style={styles.headerBtn}>
            <Text style={styles.headerBtnText}>Export</Text>
          </Pressable>
          <Pressable onPress={onClose} hitSlop={12} accessibilityRole="button" style={styles.headerBtn}>
            <Text style={styles.closeText}>Done</Text>
          </Pressable>
        </View>

        {summary.events > 0 ? <SummaryBar summary={summary} /> : null}

        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
          {FILTERS.map((f) => {
            const count = f.id === "all" ? activity.length : activity.filter((a) => a.kind === f.id).length;
            const active = filter === f.id;
            return (
              <Pressable
                key={f.id}
                onPress={() => setFilter(f.id)}
                accessibilityRole="button"
                style={[styles.chip, active && styles.chipActive]}
              >
                {f.id !== "all" ? <View style={[styles.chipDot, { backgroundColor: KIND_COLOR[f.id] }]} /> : null}
                <Text style={[styles.chipText, active && styles.chipTextActive]}>{f.label}</Text>
                {count > 0 ? <Text style={styles.chipCount}>{count}</Text> : null}
              </Pressable>
            );
          })}
        </ScrollView>

        <ScrollView
          style={styles.list}
          contentContainerStyle={styles.listContent}
          accessibilityRole="list"
          accessibilityLiveRegion="polite"
        >
          {grouped.length === 0 ? (
            <Text style={styles.empty}>
              {activity.length === 0 ? "The crew is standing by." : "No entries of this kind yet."}
            </Text>
          ) : (
            grouped.map((item) =>
              item.type === "shot" ? (
                <ShotGroupRow key={item.id} group={item} now={now} onSelectShot={onSelectShot} />
              ) : (
                <View key={item.id} style={styles.entry}>
                  <View style={styles.entryMain}>
                    <Entry item={item.activity} onSelectShot={onSelectShot} onResolveConflict={onResolveConflict} />
                  </View>
                  <Text style={styles.time}>{relativeTime(item.activity.at, now)}</Text>
                </View>
              ),
            )
          )}
        </ScrollView>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, backgroundColor: "rgba(0,0,0,0.5)" },
  sheet: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    maxHeight: "78%",
    backgroundColor: "rgba(28,18,11,0.98)",
    borderTopLeftRadius: radius.glass,
    borderTopRightRadius: radius.glass,
    borderTopWidth: 1,
    borderColor: alpha.white12,
    paddingBottom: BOTTOM_INSET,
  },
  grip: {
    alignSelf: "center",
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: alpha.white16,
    marginTop: space.sm,
    marginBottom: space.xs,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
    borderBottomWidth: 1,
    borderBottomColor: alpha.white08,
  },
  heading: { color: palette.parchment, fontFamily: fonts.display, fontSize: type.heading.fontSize },
  link: { flexDirection: "row", alignItems: "center", gap: space.xs },
  linkLabel: { fontSize: type.micro.fontSize, fontWeight: "600" },
  headerBtn: { marginLeft: "auto" },
  headerBtnText: { color: alpha.white72, fontSize: type.label.fontSize, fontWeight: "600" },
  closeText: { color: palette.emberGlow, fontSize: type.label.fontSize, fontWeight: "600" },

  summary: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: space.lg,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
    borderBottomWidth: 1,
    borderBottomColor: alpha.white08,
  },
  stat: { flexDirection: "row", alignItems: "baseline", gap: 4 },
  statValue: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "700", fontVariant: ["tabular-nums"] },
  statLabel: { color: alpha.white40, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.4 },

  dotWrap: { width: 10, height: 10, alignItems: "center", justifyContent: "center" },
  dotPulse: { position: "absolute", width: 10, height: 10, borderRadius: 5 },
  dotCore: { width: 7, height: 7, borderRadius: 3.5 },

  chips: { gap: space.xs, paddingHorizontal: space.lg, paddingVertical: space.sm, alignItems: "center" },
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: radius.pill,
    backgroundColor: alpha.white08,
  },
  chipActive: { backgroundColor: alpha.white16 },
  chipDot: { width: 6, height: 6, borderRadius: 3 },
  chipText: { color: alpha.white55, fontSize: type.caption.fontSize, fontWeight: "500" },
  chipTextActive: { color: palette.parchment },
  chipCount: { color: alpha.white40, fontSize: type.micro.fontSize, fontVariant: ["tabular-nums"] },

  list: { flexGrow: 0 },
  listContent: { paddingHorizontal: space.lg, paddingVertical: space.md, gap: space.md },
  empty: { color: alpha.white40, fontSize: type.label.fontSize, textAlign: "center", paddingVertical: space.xxxl },

  entry: { flexDirection: "row", alignItems: "flex-start", gap: space.sm },
  entryMain: { flex: 1 },
  time: { color: alpha.white40, fontSize: type.micro.fontSize, fontVariant: ["tabular-nums"], marginTop: 2 },

  group: { borderRadius: radius.md, borderWidth: 1, borderColor: alpha.white08, padding: space.xs },
  groupChild: { marginTop: space.sm },
  groupToggle: { paddingHorizontal: space.xs, paddingTop: space.xs, paddingBottom: 2 },
  groupToggleText: { color: alpha.white40, fontSize: type.micro.fontSize, fontWeight: "500" },

  row: { flexDirection: "row", alignItems: "flex-start", gap: space.sm },
  rowBody: { flex: 1 },
  rowHead: { flexDirection: "row", alignItems: "center", gap: space.xs },
  avatar: {
    width: 28,
    height: 28,
    borderRadius: 14,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: alpha.white08,
  },
  avatarText: { fontSize: 10, fontWeight: "700" },
  roleLabel: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "600" },
  rowTitle: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "600" },
  shotId: { color: alpha.white40, fontWeight: "400", fontVariant: ["tabular-nums"] },
  aspectTag: { backgroundColor: alpha.white08, borderRadius: radius.pill, paddingHorizontal: 6, paddingVertical: 1 },
  aspectText: { color: alpha.white55, fontSize: 9.5, fontWeight: "600", textTransform: "uppercase" },
  message: { color: alpha.white72, fontSize: type.label.fontSize, lineHeight: 19, marginTop: 2 },
  detail: { color: alpha.white55 },
  kindDot: { width: 8, height: 8, borderRadius: 4, marginTop: 6 },

  badgeRow: { flexDirection: "row", gap: space.xs, marginTop: 5 },
  qaBadge: { borderRadius: radius.pill, paddingHorizontal: 7, paddingVertical: 1 },
  qaText: { fontSize: 9.5, fontWeight: "700", textTransform: "uppercase" },
  ccsBadge: { backgroundColor: alpha.white08, borderRadius: radius.pill, paddingHorizontal: 7, paddingVertical: 1 },
  ccsText: { color: alpha.white72, fontSize: 9.5, fontWeight: "600", fontVariant: ["tabular-nums"] },

  conflictCard: {
    flex: 1,
    backgroundColor: "rgba(240,164,138,0.08)",
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: "rgba(240,164,138,0.22)",
    padding: space.sm,
  },
  conflictTitle: { color: palette.danger, fontSize: type.label.fontSize, fontWeight: "700" },
  canonText: { color: alpha.white40, fontSize: type.caption.fontSize, lineHeight: 17, marginTop: 4 },
  resolveBtn: {
    alignSelf: "flex-start",
    marginTop: space.sm,
    backgroundColor: palette.danger,
    borderRadius: radius.pill,
    paddingHorizontal: space.md,
    paddingVertical: 5,
  },
  resolveText: { color: palette.walnutDeep, fontSize: type.caption.fontSize, fontWeight: "700" },
});
