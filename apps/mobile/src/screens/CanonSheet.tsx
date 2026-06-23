import {
  dependentShotIds,
  ENTITY_GROUPS,
  queryKeys,
  type SessionActivity,
  type ShotResponse,
  shortShotId,
  summarizeQa,
} from "@kinora/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { CanonEntityCard } from "../components/CanonEntityCard";
import { GhostButton, SearchField, Surface } from "../components/ui";
import { api } from "../lib/api";
import { queryClient } from "../lib/queryClient";
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

interface LastEdit {
  entityName: string;
  version: number;
  affectedShotIds: string[];
  skipped: number;
}

/** Dependent shots from the last edit (§8.7), each re-rendering then flipping to
 *  ready as its `regen_done` lands in the live feed. Status chips (clips are mp4,
 *  so no per-tile player — the feed's before/after is the place to watch them). */
function DependentShots({ shotIds, activity }: { shotIds: string[]; activity: SessionActivity[] }) {
  return (
    <View style={styles.depRow}>
      {shotIds.map((id) => {
        const done = activity.find((a) => a.kind === "regen" && a.shotId === id);
        const qa = done && done.kind === "regen" ? summarizeQa(done.qa) : null;
        const ready = Boolean(done);
        return (
          <View key={id} style={[styles.depChip, ready ? styles.depChipReady : styles.depChipRegen]}>
            {ready ? <Text style={styles.depCheck}>✓</Text> : <ActivityIndicator color={palette.emberGlow} />}
            <Text style={styles.depId}>{shortShotId(id)}</Text>
            {ready && qa?.ccs != null ? <Text style={styles.depCcs}>CCS {qa.ccs.toFixed(2)}</Text> : null}
          </View>
        );
      })}
    </View>
  );
}

/**
 * The §5.4 canon editor on mobile: the §8 memory graph as a bottom sheet.
 * Entities are grouped (Characters · Locations · Props · Style); editing one and
 * saving calls `canon_edit`, then only the dependent shots it lists re-render
 * (§8.7) — everything else stays a cache hit. The header makes the bargain
 * explicit: a re-read is free, an edit is surgical.
 */
export function CanonSheet({
  visible,
  bookId,
  shots,
  activity,
  onClose,
}: {
  visible: boolean;
  bookId: string;
  shots: ShotResponse[] | undefined;
  activity: SessionActivity[];
  onClose: () => void;
}) {
  const [filter, setFilter] = useState("");
  const [lastEdit, setLastEdit] = useState<LastEdit | null>(null);

  const { data: canon, isLoading, isError } = useQuery({
    queryKey: queryKeys.canon(bookId),
    enabled: visible,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/canon", {
        params: { path: { book_id: bookId } },
      });
      if (error || !data) throw new Error("failed to load canon");
      return data;
    },
  });

  const mutation = useMutation({
    mutationFn: async (vars: { entityKey: string; changes: Record<string, unknown> }) => {
      const { data, error } = await api.POST("/api/books/{book_id}/canon_edit", {
        params: { path: { book_id: bookId } },
        body: { entity_key: vars.entityKey, changes: vars.changes },
      });
      if (error || !data) throw new Error("canon edit failed");
      return data;
    },
    onSuccess: (data) => {
      const entity = canon?.entities?.find((e) => e.id === data.entity_key);
      setLastEdit({
        entityName: entity?.name ?? data.entity_key,
        version: data.version,
        affectedShotIds: data.affected_shot_ids ?? [],
        skipped: data.skipped_shots ?? 0,
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.canon(bookId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.shots(bookId) });
    },
  });

  const entities = canon?.entities ?? [];
  const needle = filter.trim().toLowerCase();
  const visibleEntities = useMemo(
    () =>
      needle
        ? entities.filter(
            (e) =>
              e.name.toLowerCase().includes(needle) ||
              (e.aliases ?? []).some((a) => a.toLowerCase().includes(needle)),
          )
        : entities,
    [entities, needle],
  );
  const groups = ENTITY_GROUPS.map((g) => ({
    ...g,
    items: visibleEntities.filter((e) => e.type === g.type),
  })).filter((g) => g.items.length > 0);

  return (
    <Modal visible={visible} onRequestClose={onClose} transparent animationType="slide" statusBarTranslucent>
      <Pressable style={styles.scrim} onPress={onClose} accessibilityLabel="Close canon editor" />
      <View pointerEvents="box-none" style={styles.dock}>
        <Surface style={styles.card}>
          <View style={styles.handle} />
          <ScrollView contentContainerStyle={styles.content} showsVerticalScrollIndicator={false} bounces={false}>
            <Text style={styles.eyebrow}>Memory graph</Text>
            <Text style={styles.title}>Canon</Text>
            <Text style={styles.lede}>
              The story bible — versioned. A re-read is free; an edit re-renders only the shots that cite it.
            </Text>

            {lastEdit ? (
              <View style={styles.banner}>
                <Text style={styles.bannerText}>
                  Saved <Text style={styles.bannerStrong}>{lastEdit.entityName}</Text>{" "}
                  <Text style={styles.bannerVersion}>v{lastEdit.version}</Text>.{" "}
                  {lastEdit.affectedShotIds.length === 0
                    ? "No dependent shots — nothing to re-render."
                    : `${lastEdit.affectedShotIds.length} shot${lastEdit.affectedShotIds.length === 1 ? "" : "s"} re-rendering${lastEdit.skipped > 0 ? ` · ${lastEdit.skipped} untouched (cache hit)` : ""}.`}
                </Text>
                {lastEdit.affectedShotIds.length > 0 ? (
                  <DependentShots shotIds={lastEdit.affectedShotIds} activity={activity} />
                ) : null}
              </View>
            ) : null}

            {entities.length > 6 ? (
              <SearchField value={filter} onChangeText={setFilter} placeholder="Filter canon…" />
            ) : null}

            {isLoading ? (
              <View style={styles.status}>
                <ActivityIndicator color={palette.emberGlow} />
              </View>
            ) : isError ? (
              <Text style={styles.statusText}>Couldn’t load the canon graph.</Text>
            ) : entities.length === 0 ? (
              <Text style={styles.statusText}>
                No canon yet — it fills in as the book is read and shots are planned.
              </Text>
            ) : (
              groups.map((group) => (
                <View key={group.type} style={styles.section}>
                  <Text style={styles.sectionLabel}>
                    {group.label} · {group.items.length}
                  </Text>
                  <View style={styles.cards}>
                    {group.items.map((entity) => (
                      <CanonEntityCard
                        key={`${entity.id}:${entity.version}`}
                        entity={entity}
                        dependentCount={dependentShotIds(shots, entity.id).length}
                        saving={mutation.isPending && mutation.variables?.entityKey === entity.id}
                        onSave={(entityKey, changes) => mutation.mutate({ entityKey, changes })}
                      />
                    ))}
                  </View>
                </View>
              ))
            )}

            {mutation.isError ? <Text style={styles.statusText}>The edit didn’t save. Try again.</Text> : null}

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
    maxHeight: "90%",
    marginTop: TOP_INSET,
  },
  handle: { alignSelf: "center", width: 40, height: 4, borderRadius: radius.pill, backgroundColor: alpha.white16, marginBottom: space.lg },
  content: { gap: space.lg, paddingBottom: space.lg },
  eyebrow: { color: palette.emberGlow, fontSize: type.micro.fontSize, letterSpacing: 1.6, textTransform: "uppercase" },
  title: { fontFamily: fonts.display, color: palette.parchment, fontSize: type.title.fontSize, lineHeight: type.title.lineHeight, fontWeight: "600", marginTop: 2 },
  lede: { color: alpha.white55, fontSize: type.caption.fontSize, lineHeight: type.label.lineHeight, marginTop: -space.sm },
  banner: { borderRadius: radius.lg, backgroundColor: alpha.emberSoft, borderWidth: StyleSheet.hairlineWidth, borderColor: "rgba(244,168,93,0.3)", padding: space.md, gap: space.sm },
  bannerText: { color: alpha.white85, fontSize: type.caption.fontSize, lineHeight: type.label.lineHeight },
  bannerStrong: { color: palette.parchment, fontWeight: "700" },
  bannerVersion: { color: palette.emberGlow, fontVariant: ["tabular-nums"] },
  depRow: { flexDirection: "row", flexWrap: "wrap", gap: space.sm },
  depChip: { flexDirection: "row", alignItems: "center", gap: 5, borderRadius: radius.pill, paddingHorizontal: space.sm, paddingVertical: 5, borderWidth: StyleSheet.hairlineWidth },
  depChipRegen: { backgroundColor: "rgba(56,189,248,0.12)", borderColor: "rgba(56,189,248,0.35)" },
  depChipReady: { backgroundColor: "rgba(52,211,153,0.14)", borderColor: "rgba(52,211,153,0.4)" },
  depCheck: { color: "#34d399", fontSize: 12, fontWeight: "800" },
  depId: { color: alpha.white72, fontSize: type.micro.fontSize, fontVariant: ["tabular-nums"] },
  depCcs: { color: "#86efac", fontSize: type.micro.fontSize, fontWeight: "600" },
  section: { gap: space.sm },
  sectionLabel: { color: alpha.white55, fontSize: type.caption.fontSize, letterSpacing: 0.6, textTransform: "uppercase", marginLeft: 2 },
  cards: { gap: space.sm },
  status: { paddingVertical: space.xl, alignItems: "center" },
  statusText: { color: alpha.white55, fontSize: type.caption.fontSize, paddingVertical: space.md },
});
