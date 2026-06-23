import { Pressable, StyleSheet, Text, type TextStyle } from "react-native";

import { alpha, HIT_TARGET, palette, radius, type } from "../../theme/tokens";

/** A quiet, text-forward secondary action (sign out, switch mode, explore demo). */
export function GhostButton({
  label,
  onPress,
  tone = "muted",
  align = "center",
}: {
  label: string;
  onPress: () => void;
  tone?: "muted" | "ember";
  align?: "center" | "left" | "right";
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      hitSlop={8}
      style={({ pressed }) => [styles.button, pressed && styles.pressed]}
    >
      <Text
        style={[
          styles.label,
          tone === "ember" && styles.ember,
          { textAlign: align } as TextStyle,
        ]}
      >
        {label}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    minHeight: HIT_TARGET - 12,
    justifyContent: "center",
    paddingHorizontal: 6,
    borderRadius: radius.sm,
  },
  pressed: { opacity: 0.6 },
  label: { color: alpha.white72, fontSize: type.label.fontSize, fontWeight: "500" },
  ember: { color: palette.emberGlow },
});
