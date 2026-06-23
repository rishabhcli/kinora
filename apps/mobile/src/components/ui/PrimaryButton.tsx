import { ActivityIndicator, Platform, Pressable, StyleSheet, Text, View } from "react-native";

import { useReducedMotion } from "../../hooks/useReducedMotion";
import { alpha, HIT_TARGET, palette, radius, type } from "../../theme/tokens";

/**
 * The confident primary action — the desktop's ember gradient button.
 *
 * The gradient is faked with a warm ember base plus a brighter highlight bar
 * across the top third; on press it eases down to the deeper ember and (unless
 * reduce-motion is on) scales in slightly.
 */
export function PrimaryButton({
  label,
  onPress,
  busy = false,
  disabled = false,
}: {
  label: string;
  onPress: () => void;
  busy?: boolean;
  disabled?: boolean;
}) {
  const reduced = useReducedMotion();
  const isDisabled = disabled || busy;

  return (
    <Pressable
      onPress={onPress}
      disabled={isDisabled}
      accessibilityRole="button"
      accessibilityState={{ disabled: isDisabled, busy }}
      style={({ pressed }) => [
        styles.button,
        pressed && !reduced && styles.pressedTransform,
        pressed && styles.pressed,
        isDisabled && styles.disabled,
      ]}
    >
      {/* Brighter "lit" band across the top to fake the gradient sheen. */}
      <View pointerEvents="none" style={styles.sheen} />
      {busy ? (
        <ActivityIndicator color={palette.walnutDeep} />
      ) : (
        <Text style={styles.label}>{label}</Text>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    minHeight: HIT_TARGET,
    borderRadius: radius.xl,
    backgroundColor: palette.ember,
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
    ...Platform.select({
      ios: {
        shadowColor: palette.ember,
        shadowOffset: { width: 0, height: 12 },
        shadowOpacity: 0.55,
        shadowRadius: 24,
      },
      android: { elevation: 8 },
      default: {},
    }),
  },
  sheen: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    height: "48%",
    backgroundColor: alpha.emberGlowSheen,
  },
  pressed: { backgroundColor: palette.emberDeep },
  pressedTransform: { transform: [{ scale: 0.985 }] },
  disabled: { opacity: 0.55 },
  label: {
    color: palette.walnutDeep,
    fontSize: type.heading.fontSize,
    fontWeight: "700",
    letterSpacing: 0.2,
  },
});
