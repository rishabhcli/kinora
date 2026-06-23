import type { ReactNode } from "react";
import { Platform, StyleSheet, View, type ViewStyle } from "react-native";

import { alpha, radius } from "../../theme/tokens";

/**
 * A frosted-glass panel — the desktop's `.glass-strong` translated to RN.
 *
 * Without a backdrop-blur primitive we lean on a warm translucent fill, a 1px
 * hairline border, a bright inset-style top highlight (a thin overlay bar) and a
 * soft drop shadow, which together read as a floating glass card over the
 * ambient room.
 */
export function Surface({
  children,
  style,
  radius: r = radius.glass,
}: {
  children?: ReactNode;
  style?: ViewStyle | ViewStyle[];
  radius?: number;
}) {
  return (
    <View style={[styles.surface, { borderRadius: r }, style]}>
      {/* The specular top edge that sells the glass. */}
      <View pointerEvents="none" style={[styles.specular, { borderTopLeftRadius: r, borderTopRightRadius: r }]} />
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  surface: {
    backgroundColor: alpha.glassFill,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white16,
    overflow: "hidden",
    ...Platform.select({
      ios: {
        shadowColor: "#000",
        shadowOffset: { width: 0, height: 18 },
        shadowOpacity: 0.45,
        shadowRadius: 40,
      },
      android: { elevation: 14 },
      default: {},
    }),
  },
  specular: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    height: 1.5,
    backgroundColor: alpha.specular,
  },
});
