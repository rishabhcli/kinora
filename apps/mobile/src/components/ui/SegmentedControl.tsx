import { Pressable, StyleSheet, Text, View } from "react-native";

import { alpha, palette, radius, type } from "../../theme/tokens";

/**
 * An iOS-style segmented control used on phones to flip between the reading
 * column and the film. Compact, glassy, with a warm parchment "thumb" behind
 * the active segment.
 */
export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <View style={styles.track} accessibilityRole="tablist">
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <Pressable
            key={opt.value}
            onPress={() => onChange(opt.value)}
            accessibilityRole="tab"
            accessibilityState={{ selected: active }}
            style={[styles.segment, active && styles.segmentActive]}
          >
            <Text style={[styles.label, active && styles.labelActive]}>{opt.label}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  track: {
    flexDirection: "row",
    padding: 3,
    borderRadius: radius.pill,
    backgroundColor: alpha.glassFillSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white12,
  },
  segment: {
    flex: 1,
    paddingVertical: 8,
    borderRadius: radius.pill,
    alignItems: "center",
    justifyContent: "center",
  },
  segmentActive: { backgroundColor: palette.parchment },
  label: { color: alpha.white72, fontSize: type.label.fontSize, fontWeight: "600" },
  labelActive: { color: palette.walnutDeep },
});
