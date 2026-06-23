import { importGateMessage, type BookResponse } from "@kinora/core";
import { Modal, Pressable, StyleSheet, Text, View } from "react-native";

import { alpha, fonts, palette, radius, space, type } from "../theme/tokens";
import { Surface } from "./ui";

/** Sheet shown when the reader taps a book that is still importing or failed. */
export function ImportGateSheet({
  book,
  visible,
  onClose,
}: {
  book: BookResponse;
  visible: boolean;
  onClose: () => void;
}) {
  const { title, body } = importGateMessage(book);
  const failed = book.status === "failed";

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <Pressable style={styles.scrim} onPress={onClose}>
        <Pressable onPress={(event) => event.stopPropagation()}>
          <Surface style={styles.card}>
            <View style={[styles.icon, failed ? styles.iconFailed : styles.iconWorking]} />
            <Text style={styles.title}>{title}</Text>
            <Text style={styles.body}>{body}</Text>
            <Text style={styles.bookTitle}>{book.title}</Text>
            <Pressable onPress={onClose} style={styles.button} accessibilityRole="button">
              <Text style={styles.buttonLabel}>Back to the shelf</Text>
            </Pressable>
          </Surface>
        </Pressable>
      </Pressable>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: {
    flex: 1,
    backgroundColor: "rgba(0,0,0,0.55)",
    alignItems: "center",
    justifyContent: "center",
    padding: space.xl,
  },
  card: { width: "100%", maxWidth: 360, padding: space.xxl, alignItems: "center" },
  icon: {
    width: 44,
    height: 44,
    borderRadius: radius.pill,
    marginBottom: space.md,
  },
  iconFailed: { backgroundColor: "rgba(244,63,94,0.18)" },
  iconWorking: { backgroundColor: alpha.emberSoft },
  title: {
    fontFamily: fonts.display,
    color: palette.parchment,
    fontSize: type.title.fontSize,
    fontWeight: "600",
    textAlign: "center",
  },
  body: {
    color: alpha.white55,
    fontSize: type.label.fontSize,
    lineHeight: type.body.lineHeight,
    textAlign: "center",
    marginTop: space.sm,
  },
  bookTitle: {
    fontFamily: fonts.display,
    color: alpha.white72,
    fontSize: type.label.fontSize,
    textAlign: "center",
    marginTop: space.md,
  },
  button: {
    marginTop: space.lg,
    width: "100%",
    borderRadius: radius.lg,
    backgroundColor: alpha.white12,
    paddingVertical: space.md,
    alignItems: "center",
  },
  buttonLabel: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "600" },
});
