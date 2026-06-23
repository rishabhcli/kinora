import {
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  View,
} from "react-native";

import { GhostButton, PrimaryButton, Surface } from "../components/ui";
import { useAuth } from "../hooks/useAuth";
import { usePreferences } from "../hooks/usePreferences";
import { preferencesStore } from "../lib/preferences";
import {
  alpha,
  BOTTOM_INSET,
  fonts,
  HIT_TARGET,
  palette,
  radius,
  space,
  TOP_INSET,
  type,
} from "../theme/tokens";

/** One labelled preference row with a native Switch tinted to the warm palette. */
function ToggleRow({
  title,
  description,
  value,
  onValueChange,
}: {
  title: string;
  description: string;
  value: boolean;
  onValueChange: (value: boolean) => void;
}) {
  return (
    <Pressable
      onPress={() => onValueChange(!value)}
      accessibilityRole="switch"
      accessibilityState={{ checked: value }}
      accessibilityLabel={title}
      style={styles.row}
    >
      <View style={styles.rowText}>
        <Text style={styles.rowTitle}>{title}</Text>
        <Text style={styles.rowDescription}>{description}</Text>
      </View>
      <Switch
        value={value}
        onValueChange={onValueChange}
        // Match the desktop ember; the "off" track stays a quiet glass tint.
        trackColor={{ false: "rgba(255,255,255,0.16)", true: palette.emberDeep }}
        thumbColor={Platform.OS === "android" ? (value ? palette.emberGlow : palette.parchment) : undefined}
        ios_backgroundColor="rgba(255,255,255,0.12)"
      />
    </Pressable>
  );
}

/**
 * The profile / settings sheet — a glass card sliding up over the reading room.
 * Shows the signed-in account, two persisted preferences (reduce-motion override
 * + autoplay), and a Sign out action that clears the token through the existing
 * auth flow (passed in from the library so the screen swap happens there).
 */
export function SettingsSheet({
  visible,
  onClose,
  onSignOut,
}: {
  visible: boolean;
  onClose: () => void;
  onSignOut: () => void;
}) {
  const email = useAuth((state) => state.user?.email);
  const reduceMotionOverride = usePreferences((state) => state.reduceMotionOverride);
  const autoplayOnOpen = usePreferences((state) => state.autoplayOnOpen);

  return (
    <Modal
      visible={visible}
      onRequestClose={onClose}
      transparent
      animationType="slide"
      statusBarTranslucent
    >
      {/* Dim scrim — tapping outside the card dismisses the sheet. */}
      <Pressable style={styles.scrim} onPress={onClose} accessibilityLabel="Close settings" />
      <View pointerEvents="box-none" style={styles.dock}>
        <Surface style={styles.card}>
          {/* A small grab handle to read as a bottom sheet. */}
          <View style={styles.handle} />
          <ScrollView
            contentContainerStyle={styles.content}
            showsVerticalScrollIndicator={false}
            bounces={false}
          >
            <Text style={styles.eyebrow}>Account</Text>
            <Text style={styles.title}>Settings</Text>

            <View style={styles.account}>
              <View style={styles.avatar}>
                <Text style={styles.avatarGlyph}>
                  {(email?.[0] ?? "K").toUpperCase()}
                </Text>
              </View>
              <View style={styles.accountText}>
                <Text style={styles.accountLabel}>Signed in as</Text>
                <Text style={styles.accountEmail} numberOfLines={1}>
                  {email ?? "Reader"}
                </Text>
              </View>
            </View>

            <View style={styles.section}>
              <Text style={styles.sectionLabel}>Preferences</Text>
              <View style={styles.toggles}>
                <ToggleRow
                  title="Reduce motion"
                  description="Quiet animated transitions, even if the system setting is off."
                  value={reduceMotionOverride}
                  onValueChange={(next) =>
                    preferencesStore.getState().set("reduceMotionOverride", next)
                  }
                />
                <View style={styles.divider} />
                <ToggleRow
                  title="Autoplay film"
                  description="Start the film automatically when you open a book."
                  value={autoplayOnOpen}
                  onValueChange={(next) =>
                    preferencesStore.getState().set("autoplayOnOpen", next)
                  }
                />
              </View>
            </View>

            <View style={styles.actions}>
              <PrimaryButton label="Sign out" onPress={onSignOut} />
              <GhostButton label="Close" onPress={onClose} />
            </View>
          </ScrollView>
        </Surface>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: { ...StyleSheet.absoluteFill, backgroundColor: "rgba(8,5,3,0.62)" },
  dock: { flex: 1, justifyContent: "flex-end" },
  card: {
    borderBottomLeftRadius: 0,
    borderBottomRightRadius: 0,
    paddingTop: space.md,
    paddingHorizontal: space.xxl,
    paddingBottom: BOTTOM_INSET + space.lg,
    maxHeight: "88%",
    // Keep the sheet's top clear of the notch on tall content.
    marginTop: TOP_INSET,
  },
  handle: {
    alignSelf: "center",
    width: 40,
    height: 4,
    borderRadius: radius.pill,
    backgroundColor: alpha.white16,
    marginBottom: space.lg,
  },
  content: { gap: space.lg },
  eyebrow: {
    color: palette.emberGlow,
    fontSize: type.micro.fontSize,
    letterSpacing: 1.6,
    textTransform: "uppercase",
  },
  title: {
    fontFamily: fonts.display,
    color: palette.parchment,
    fontSize: type.title.fontSize,
    lineHeight: type.title.lineHeight,
    fontWeight: "600",
    marginTop: 2,
  },
  account: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.md,
    paddingVertical: space.sm,
  },
  avatar: {
    width: 48,
    height: 48,
    borderRadius: radius.pill,
    backgroundColor: alpha.emberSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white16,
    alignItems: "center",
    justifyContent: "center",
  },
  avatarGlyph: {
    fontFamily: fonts.display,
    color: palette.emberGlow,
    fontSize: type.title.fontSize,
    fontWeight: "600",
  },
  accountText: { flex: 1, gap: 1 },
  accountLabel: {
    color: alpha.white40,
    fontSize: type.caption.fontSize,
    letterSpacing: 0.4,
    textTransform: "uppercase",
  },
  accountEmail: { color: alpha.white95, fontSize: type.heading.fontSize, fontWeight: "600" },
  section: { gap: space.sm },
  sectionLabel: {
    color: alpha.white55,
    fontSize: type.caption.fontSize,
    letterSpacing: 0.6,
    textTransform: "uppercase",
    marginLeft: 2,
  },
  toggles: {
    borderRadius: radius.lg,
    backgroundColor: alpha.glassFillSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white12,
    paddingHorizontal: space.lg,
  },
  row: {
    minHeight: HIT_TARGET + 8,
    flexDirection: "row",
    alignItems: "center",
    gap: space.lg,
    paddingVertical: space.md,
  },
  rowText: { flex: 1, gap: 2 },
  rowTitle: { color: palette.parchment, fontSize: type.body.fontSize, fontWeight: "600" },
  rowDescription: {
    color: alpha.white55,
    fontSize: type.caption.fontSize,
    lineHeight: type.label.lineHeight,
  },
  divider: { height: StyleSheet.hairlineWidth, backgroundColor: alpha.white12 },
  actions: { gap: space.sm, marginTop: space.sm },
});
