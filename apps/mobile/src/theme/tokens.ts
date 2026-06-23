import { Platform, StatusBar } from "react-native";

/**
 * Kinora's mobile design language — the warm, literary "watch the book" identity
 * carried over from the desktop shell (apps/desktop/tailwind.config.js + index.css).
 *
 * We have no LinearGradient / BlurView dependency on mobile, so the glassy,
 * gradient look is composed from layered translucent `View`s and these tokens.
 */

/** The warm-library palette (mirrors the desktop Tailwind theme). */
export const palette = {
  walnut: "#241811",
  walnutDeep: "#160e08",
  walnutWall: "#1d130c",
  oak: "#7a512f",
  oakLight: "#9c6d40",
  oakDark: "#5a3c22",
  parchment: "#f3ecdd",
  parchmentWarm: "#efe5cf",
  ink: "#1c150f",
  inkSoft: "#5b4d40",
  inkFaint: "#9b8a78",
  ember: "#e0863a",
  emberDeep: "#c26a24",
  emberGlow: "#f4a85d",
  danger: "#f0a48a",
} as const;

/** Translucent ink/parchment tints used for glass surfaces, borders and text. */
export const alpha = {
  /** Warm white text at decreasing emphasis. */
  white95: "rgba(255,255,255,0.95)",
  white85: "rgba(255,255,255,0.85)",
  white72: "rgba(255,255,255,0.72)",
  white55: "rgba(255,255,255,0.55)",
  white40: "rgba(255,255,255,0.40)",
  white16: "rgba(255,255,255,0.16)",
  white12: "rgba(255,255,255,0.12)",
  white08: "rgba(255,255,255,0.08)",
  /** The specular top edge that sells the glass. */
  specular: "rgba(255,255,255,0.30)",
  /** Frosted card fills (over the warm walnut backdrop). */
  glassFill: "rgba(46,30,18,0.55)",
  glassFillSoft: "rgba(255,255,255,0.06)",
  glassFieldFocus: "rgba(244,168,93,0.65)",
  emberSoft: "rgba(224,134,58,0.16)",
  /** The brighter "lit" band layered over the ember button to fake a gradient. */
  emberGlowSheen: "rgba(255,221,176,0.35)",
} as const;

/**
 * Type faces. The desktop wordmark + headings use a serif display face; body is
 * system sans. On both iOS and Android `Georgia` is a safe, warm serif, so we
 * lean on it for the display face (no font-loading dependency needed).
 */
export const fonts = {
  display: Platform.select({ ios: "Georgia", android: "serif", default: "Georgia" }),
  /** System sans — RN's default; left undefined so the platform face is used. */
  sans: undefined as string | undefined,
} as const;

/** A deliberate type scale (sizes paired with their natural line-heights). */
export const type = {
  wordmark: { fontSize: 46, lineHeight: 48 },
  display: { fontSize: 30, lineHeight: 36 },
  title: { fontSize: 22, lineHeight: 28 },
  heading: { fontSize: 17, lineHeight: 22 },
  body: { fontSize: 16, lineHeight: 26 },
  reading: { fontSize: 19, lineHeight: 32 },
  label: { fontSize: 14, lineHeight: 20 },
  caption: { fontSize: 12, lineHeight: 16 },
  micro: { fontSize: 11, lineHeight: 14 },
} as const;

/** An 8pt-ish spacing rhythm. */
export const space = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 20,
  xxl: 24,
  xxxl: 32,
  huge: 44,
} as const;

export const radius = {
  sm: 10,
  md: 14,
  lg: 18,
  xl: 22,
  glass: 26,
  pill: 999,
} as const;

/** A minimum comfortable hit target (iOS HIG / Material both ~44–48). */
export const HIT_TARGET = 48;

/**
 * A pragmatic top inset for the notch/status bar without pulling in
 * react-native-safe-area-context (which isn't a dependency). Good enough for a
 * polished layout; a production build would adopt SafeAreaProvider.
 */
export const TOP_INSET = Platform.select({
  ios: 56,
  android: (StatusBar.currentHeight ?? 24) + 12,
  default: 24,
});

export const BOTTOM_INSET = Platform.select({ ios: 28, android: 16, default: 16 });

/** The width below which we treat the device as a phone (stacked layouts). */
export const TABLET_BREAKPOINT = 720;
