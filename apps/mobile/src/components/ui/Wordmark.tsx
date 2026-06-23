import { StyleSheet, Text, View } from "react-native";

import { alpha, fonts, palette, type } from "../../theme/tokens";

/**
 * The Kinora mark: a small ember "film frame" (a rounded square with two
 * sprocket notches) that doubles as a closed book — drawn with plain Views so
 * we need no icon/SVG dependency.
 */
export function KinoraMark({ size = 30 }: { size?: number }) {
  const r = size * 0.26;
  const notch = size * 0.13;
  return (
    <View style={[markStyles.frame, { width: size, height: size, borderRadius: r }]}>
      <View style={[markStyles.notch, { width: notch, height: notch * 1.6, top: size * 0.5 - notch * 0.8, left: -notch / 2 }]} />
      <View style={[markStyles.notch, { width: notch, height: notch * 1.6, top: size * 0.5 - notch * 0.8, right: -notch / 2 }]} />
      {/* The book "spine" down the centre of the frame. */}
      <View style={[markStyles.spine, { width: Math.max(1.5, size * 0.06) }]} />
    </View>
  );
}

/** The full wordmark: the mark + serif "Kinora", optionally with the tagline. */
export function Wordmark({
  size = type.wordmark.fontSize,
  withMark = true,
  withTagline = false,
  align = "center",
}: {
  size?: number;
  withMark?: boolean;
  withTagline?: boolean;
  align?: "center" | "left";
}) {
  return (
    <View style={{ alignItems: align === "center" ? "center" : "flex-start" }}>
      <View style={[wordStyles.row, align === "center" && wordStyles.rowCenter]}>
        {withMark ? <KinoraMark size={size * 0.62} /> : null}
        <Text
          style={[
            wordStyles.word,
            { fontSize: size, lineHeight: size * 1.04, marginLeft: withMark ? size * 0.26 : 0 },
          ]}
        >
          Kinora
        </Text>
      </View>
      {withTagline ? <Text style={wordStyles.tagline}>Watch the book.</Text> : null}
    </View>
  );
}

const markStyles = StyleSheet.create({
  frame: {
    borderWidth: 2,
    borderColor: palette.emberGlow,
    backgroundColor: alpha.emberSoft,
    alignItems: "center",
    justifyContent: "center",
  },
  notch: {
    position: "absolute",
    backgroundColor: palette.walnut,
    borderRadius: 2,
    borderWidth: 1.5,
    borderColor: palette.emberGlow,
  },
  spine: {
    height: "62%",
    backgroundColor: palette.emberGlow,
    borderRadius: 2,
    opacity: 0.9,
  },
});

const wordStyles = StyleSheet.create({
  row: { flexDirection: "row", alignItems: "center" },
  rowCenter: { justifyContent: "center" },
  word: {
    fontFamily: fonts.display,
    color: palette.parchment,
    fontWeight: "600",
    letterSpacing: -0.5,
  },
  tagline: {
    marginTop: 10,
    color: alpha.white55,
    fontSize: type.body.fontSize,
    letterSpacing: 0.2,
  },
});
