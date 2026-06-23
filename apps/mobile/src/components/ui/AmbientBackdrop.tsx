import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";

import { palette } from "../../theme/tokens";

/**
 * The warm "screening room" backdrop behind every screen: a deep walnut base
 * with a soft ember projector wash bleeding down from the top and a floor
 * vignette settling into the dark.
 *
 * We have no gradient primitive on mobile, so the wash is built from a stack of
 * large, low-opacity radial-ish layers — translucent rounded panels that read
 * as light falling through the frame. Pointer-events pass straight through.
 */
export function AmbientBackdrop({ children }: { children?: ReactNode }) {
  return (
    <View style={styles.root}>
      {/* Ember wash from above (a wide, soft glow centred over the top edge). */}
      <View pointerEvents="none" style={styles.washWrap}>
        <View style={styles.washOuter} />
        <View style={styles.washInner} />
      </View>
      {/* Floor vignette: the room recedes into near-black at the base. */}
      <View pointerEvents="none" style={styles.vignette} />
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: palette.walnut },
  washWrap: {
    position: "absolute",
    top: -260,
    left: -120,
    right: -120,
    height: 560,
    alignItems: "center",
  },
  washOuter: {
    position: "absolute",
    top: 0,
    width: 720,
    height: 560,
    borderRadius: 360,
    backgroundColor: "rgba(224,134,58,0.10)",
  },
  washInner: {
    position: "absolute",
    top: 90,
    width: 460,
    height: 380,
    borderRadius: 230,
    backgroundColor: "rgba(244,168,93,0.12)",
  },
  vignette: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    height: "55%",
    backgroundColor: palette.walnutDeep,
    opacity: 0.55,
  },
});
