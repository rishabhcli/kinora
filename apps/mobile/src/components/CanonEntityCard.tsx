import { type CanonEntityResponse, buildChanges, draftFromEntity, type EntityDraft } from "@kinora/core";
import { useState } from "react";
import { Image, Pressable, StyleSheet, Text, View } from "react-native";

import { alpha, fonts, palette, radius, space, type } from "../theme/tokens";
import { GhostButton, GlassField, PrimaryButton } from "./ui";

const TYPE_TINT: Record<string, string> = {
  character: "rgba(224,134,58,0.20)",
  location: "rgba(52,211,153,0.18)",
  prop: "rgba(96,165,250,0.18)",
  style: "rgba(167,139,250,0.18)",
};

/** One reference image — tap to lock/unlock it in the canonical set (§8.1). */
function RefThumb({ url, pose, locked, onToggle }: {
  url: string;
  pose: string | null;
  locked: boolean;
  onToggle: () => void;
}) {
  return (
    <Pressable
      onPress={onToggle}
      accessibilityRole="button"
      accessibilityState={{ selected: locked }}
      accessibilityLabel={`${locked ? "Unlock" : "Lock"} reference${pose ? ` ${pose}` : ""}`}
      style={[styles.thumb, locked ? styles.thumbLocked : styles.thumbUnlocked]}
    >
      <Image source={{ uri: url }} resizeMode="cover" style={[styles.thumbImg, !locked && styles.thumbImgDim]} />
      <View style={[styles.lockChip, locked ? styles.lockChipOn : styles.lockChipOff]}>
        <Text style={[styles.lockGlyph, locked && styles.lockGlyphOn]}>{locked ? "🔒" : "🔓"}</Text>
      </View>
      {pose ? (
        <View style={styles.poseTag}>
          <Text style={styles.poseText}>{pose}</Text>
        </View>
      ) : null}
    </Pressable>
  );
}

/**
 * One canon entity, inspectable + editable (§5.4). Edit the name, aliases,
 * appearance description, locked reference set, or — for a Style node — its
 * palette/lens/art-direction. Save diffs to a minimal `canon_edit` `changes`
 * map; the version chip + "N shots" count make the surgical blast radius legible.
 */
export function CanonEntityCard({ entity, dependentCount, saving, onSave }: {
  entity: CanonEntityResponse;
  dependentCount: number;
  saving: boolean;
  onSave: (entityKey: string, changes: Record<string, unknown>) => void;
}) {
  const [draft, setDraft] = useState<EntityDraft>(() => draftFromEntity(entity));
  const [expanded, setExpanded] = useState(false);

  const changes = buildChanges(entity, draft);
  const dirty = Object.keys(changes).length > 0;
  const set = (patch: Partial<EntityDraft>): void => setDraft((d) => ({ ...d, ...patch }));
  const toggleLock = (index: number): void =>
    setDraft((d) => ({
      ...d,
      references: d.references.map((r, i) => (i === index ? { ...r, locked: !r.locked } : r)),
    }));

  const showAppearance =
    entity.type === "character" ||
    entity.type === "location" ||
    entity.type === "prop" ||
    draft.references.length > 0;

  return (
    <View style={[styles.card, dirty && styles.cardDirty]}>
      <Pressable
        onPress={() => setExpanded((v) => !v)}
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        style={styles.cardHead}
      >
        <Text style={[styles.chevron, expanded && styles.chevronOpen]}>›</Text>
        <Text style={styles.cardName} numberOfLines={1}>{draft.name || entity.name}</Text>
        {dirty ? <View style={styles.dirtyDot} /> : null}
        <View style={[styles.typeChip, { backgroundColor: TYPE_TINT[entity.type] ?? alpha.white12 }]}>
          <Text style={styles.typeChipText}>{entity.type}</Text>
        </View>
        <Text style={styles.versionChip}>v{entity.version}</Text>
      </Pressable>

      {expanded ? (
        <View style={styles.cardBody}>
          <Text style={styles.blast}>
            {dependentCount === 0
              ? "No shots cite this yet — an edit re-renders nothing."
              : `${dependentCount} shot${dependentCount === 1 ? "" : "s"} cite this — an edit re-renders only ${dependentCount === 1 ? "it" : "those"}.`}
          </Text>

          <GlassField label="Name" value={draft.name} onChangeText={(t) => set({ name: t })} />
          <GlassField
            label="Aliases"
            value={draft.aliasesText}
            onChangeText={(t) => set({ aliasesText: t })}
            placeholder="comma, separated"
            autoCapitalize="none"
          />
          <GlassField
            label="Description"
            value={draft.description}
            onChangeText={(t) => set({ description: t })}
            multiline
            style={styles.multiline}
          />

          {showAppearance ? (
            <>
              <GlassField
                label="Appearance"
                value={draft.appearanceDescription}
                onChangeText={(t) => set({ appearanceDescription: t })}
                placeholder="the canonical look — features, wardrobe, palette"
                multiline
                style={styles.multiline}
              />
              {draft.references.length > 0 ? (
                <View style={styles.refs}>
                  {draft.references.map((ref, i) => (
                    <RefThumb
                      key={ref.ossKey ?? ref.ossUrl}
                      url={ref.ossUrl}
                      pose={ref.pose}
                      locked={ref.locked}
                      onToggle={() => toggleLock(i)}
                    />
                  ))}
                </View>
              ) : null}
            </>
          ) : null}

          {entity.type === "style" ? (
            <>
              <GlassField
                label="Palette"
                value={draft.palette.join(", ")}
                onChangeText={(t) =>
                  set({ palette: t.split(/[\s,]+/).map((c) => c.trim()).filter(Boolean) })
                }
                placeholder="#1b2a4a, #c97b4a, ivory"
                autoCapitalize="none"
              />
              {draft.palette.length > 0 ? (
                <View style={styles.swatches}>
                  {draft.palette.map((color, i) => (
                    <View key={`${color}-${i}`} style={styles.swatchRow}>
                      <View style={[styles.swatch, { backgroundColor: color }]} />
                      <Text style={styles.swatchLabel}>{color}</Text>
                    </View>
                  ))}
                </View>
              ) : null}
              <GlassField label="Lens" value={draft.lens} onChangeText={(t) => set({ lens: t })} placeholder="35mm anamorphic" />
              <GlassField
                label="Art direction"
                value={draft.artDirection}
                onChangeText={(t) => set({ artDirection: t })}
                placeholder="painterly storybook, warm key light"
                multiline
                style={styles.multiline}
              />
            </>
          ) : null}

          <View style={styles.cardActions}>
            {dirty ? <GhostButton label="Reset" onPress={() => setDraft(draftFromEntity(entity))} /> : null}
            <PrimaryButton
              label={saving ? "Saving" : "Save & re-render"}
              busy={saving}
              disabled={!dirty}
              onPress={() => onSave(entity.id, changes)}
            />
          </View>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    borderRadius: radius.lg,
    backgroundColor: alpha.glassFillSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white12,
    overflow: "hidden",
  },
  cardDirty: { borderColor: palette.emberDeep },
  cardHead: { flexDirection: "row", alignItems: "center", gap: space.sm, paddingHorizontal: space.md, paddingVertical: space.md },
  chevron: { color: alpha.white40, fontSize: 20, width: 14 },
  chevronOpen: { color: palette.emberGlow },
  cardName: { flex: 1, color: palette.parchment, fontSize: type.body.fontSize, fontWeight: "600" },
  dirtyDot: { width: 7, height: 7, borderRadius: 3.5, backgroundColor: palette.emberGlow },
  typeChip: { paddingHorizontal: space.sm, paddingVertical: 2, borderRadius: radius.pill },
  typeChipText: { color: alpha.white72, fontSize: type.micro.fontSize, fontWeight: "700", textTransform: "uppercase", letterSpacing: 0.4 },
  versionChip: { color: alpha.white55, fontSize: type.caption.fontSize, fontVariant: ["tabular-nums"] },
  cardBody: { paddingHorizontal: space.md, paddingBottom: space.md, gap: space.md, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: alpha.white08, paddingTop: space.md },
  blast: { color: alpha.white55, fontSize: type.caption.fontSize, lineHeight: type.label.lineHeight },
  multiline: { minHeight: 64, textAlignVertical: "top", paddingTop: 12 },
  refs: { flexDirection: "row", flexWrap: "wrap", gap: space.sm },
  thumb: { width: 76, height: 76, borderRadius: radius.md, overflow: "hidden", borderWidth: 2 },
  thumbLocked: { borderColor: palette.emberGlow },
  thumbUnlocked: { borderColor: alpha.white12 },
  thumbImg: { width: "100%", height: "100%" },
  thumbImgDim: { opacity: 0.5 },
  lockChip: { position: "absolute", top: 3, right: 3, width: 18, height: 18, borderRadius: 9, alignItems: "center", justifyContent: "center" },
  lockChipOn: { backgroundColor: palette.emberGlow },
  lockChipOff: { backgroundColor: "rgba(0,0,0,0.55)" },
  lockGlyph: { fontSize: 9 },
  lockGlyphOn: {},
  poseTag: { position: "absolute", bottom: 3, left: 3, backgroundColor: "rgba(0,0,0,0.6)", borderRadius: 4, paddingHorizontal: 4, paddingVertical: 1 },
  poseText: { color: alpha.white85, fontSize: 9, fontWeight: "600" },
  swatches: { flexDirection: "row", flexWrap: "wrap", gap: space.sm },
  swatchRow: { flexDirection: "row", alignItems: "center", gap: 5, backgroundColor: alpha.white08, borderRadius: radius.pill, paddingLeft: 4, paddingRight: space.sm, paddingVertical: 3 },
  swatch: { width: 16, height: 16, borderRadius: 8, borderWidth: StyleSheet.hairlineWidth, borderColor: alpha.white16 },
  swatchLabel: { color: alpha.white72, fontSize: type.micro.fontSize, fontVariant: ["tabular-nums"] },
  cardActions: { flexDirection: "row", justifyContent: "flex-end", alignItems: "center", gap: space.sm, marginTop: space.xs },
});
