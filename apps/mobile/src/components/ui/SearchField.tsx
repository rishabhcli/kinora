import { StyleSheet, TextInput, View } from "react-native";

import { alpha, palette, radius, type } from "../../theme/tokens";

/** A drawn magnifier glyph (a ringed circle + handle) — no icon dependency. */
function SearchGlyph() {
  return (
    <View style={glyph.wrap}>
      <View style={glyph.ring} />
      <View style={glyph.handle} />
    </View>
  );
}

/** A glassy search pill for filtering the library. */
export function SearchField({
  value,
  onChangeText,
  placeholder = "Search your library",
}: {
  value: string;
  onChangeText: (text: string) => void;
  placeholder?: string;
}) {
  return (
    <View style={styles.pill}>
      <SearchGlyph />
      <TextInput
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={alpha.white40}
        autoCapitalize="none"
        autoCorrect={false}
        returnKeyType="search"
        style={styles.input}
        accessibilityLabel="Search your library"
      />
    </View>
  );
}

const styles = StyleSheet.create({
  pill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 14,
    height: 44,
    borderRadius: radius.pill,
    backgroundColor: alpha.glassFillSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white16,
  },
  input: { flex: 1, color: palette.parchment, fontSize: type.label.fontSize, paddingVertical: 0 },
});

const glyph = StyleSheet.create({
  wrap: { width: 16, height: 16 },
  ring: {
    width: 12,
    height: 12,
    borderRadius: 6,
    borderWidth: 1.6,
    borderColor: alpha.white55,
  },
  handle: {
    position: "absolute",
    right: 0,
    bottom: 0,
    width: 6,
    height: 1.6,
    borderRadius: 1,
    backgroundColor: alpha.white55,
    transform: [{ rotate: "45deg" }],
  },
});
