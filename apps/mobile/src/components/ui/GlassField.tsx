import { useState } from "react";
import { StyleSheet, Text, TextInput, type TextInputProps, View } from "react-native";

import { alpha, HIT_TARGET, palette, radius, type } from "../../theme/tokens";

/**
 * A glassy text field — the desktop `.glass-input`: a faint translucent fill
 * and hairline border that warms to an ember edge on focus. A small floating
 * label sits above the field for clarity on a busy backdrop.
 */
export function GlassField({
  label,
  style,
  ...inputProps
}: { label: string } & TextInputProps) {
  const [focused, setFocused] = useState(false);
  return (
    <View style={styles.wrap}>
      <Text style={styles.label}>{label}</Text>
      <TextInput
        {...inputProps}
        onFocus={(e) => {
          setFocused(true);
          inputProps.onFocus?.(e);
        }}
        onBlur={(e) => {
          setFocused(false);
          inputProps.onBlur?.(e);
        }}
        placeholderTextColor={alpha.white40}
        style={[styles.input, focused && styles.inputFocused, style]}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { gap: 6 },
  label: {
    color: alpha.white55,
    fontSize: type.caption.fontSize,
    letterSpacing: 0.6,
    textTransform: "uppercase",
    marginLeft: 4,
  },
  input: {
    minHeight: HIT_TARGET,
    borderRadius: radius.md,
    paddingHorizontal: 16,
    paddingVertical: 12,
    backgroundColor: alpha.glassFillSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white16,
    color: palette.parchment,
    fontSize: type.body.fontSize,
  },
  inputFocused: {
    borderColor: alpha.glassFieldFocus,
    backgroundColor: alpha.white12,
  },
});
