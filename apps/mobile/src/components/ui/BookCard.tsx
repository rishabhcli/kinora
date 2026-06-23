import { type BookResponse, queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useRef } from "react";
import {
  Animated,
  Image,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { useReducedMotion } from "../../hooks/useReducedMotion";
import { api } from "../../lib/api";
import { alpha, fonts, palette, type } from "../../theme/tokens";

/** Warm literary spine colours for cover-less books (deterministic per id). */
const SPINES = ["#3a2a4f", "#1f3a5f", "#3a1212", "#2b3b2e", "#4a3a2a", "#163b46"];
function spineColor(id: string): string {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return SPINES[h % SPINES.length] ?? SPINES[0]!;
}

function stageLabel(book: BookResponse): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

function progressPct(book: BookResponse): number {
  const raw = book.progress;
  if (raw == null || Number.isNaN(raw)) return 0;
  return Math.round(Math.min(1, Math.max(0, raw)) * 100);
}

/**
 * A book standing on the shelf — its page-1 image as the cover when the book is
 * `ready`, otherwise a titled spine card in a warm hue. The bound spine edge, a
 * diagonal sheen and a soft shadow give it depth; a status pill shows while a
 * book is still being adapted. Pressing lifts it (unless reduce-motion is on).
 */
export function BookCard({
  book,
  width,
  onPress,
}: {
  book: BookResponse;
  width: number;
  onPress: () => void;
}) {
  const reduced = useReducedMotion();
  const lift = useRef(new Animated.Value(0)).current;
  const ready = book.status === "ready";
  const failed = book.status === "failed";
  const working = !ready && !failed;
  const pct = progressPct(book);

  const { data } = useQuery({
    queryKey: queryKeys.page(book.id, 1),
    enabled: ready,
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: book.id, page_number: 1 } },
      });
      return error || !data ? null : data;
    },
  });
  const cover = data?.image_url ?? null;

  function animate(to: number) {
    if (reduced) return;
    Animated.spring(lift, { toValue: to, useNativeDriver: true, speed: 18, bounciness: 6 }).start();
  }

  const translateY = lift.interpolate({ inputRange: [0, 1], outputRange: [0, -8] });

  return (
    <Pressable
      onPress={ready ? onPress : undefined}
      disabled={!ready}
      onPressIn={() => ready && animate(1)}
      onPressOut={() => ready && animate(0)}
      accessibilityRole="button"
      accessibilityState={{ disabled: !ready }}
      accessibilityLabel={
        ready
          ? `Open ${book.title}${book.author ? `, by ${book.author}` : ""}`
          : `${book.title} is still importing`
      }
      style={{ width, opacity: ready ? 1 : 0.82 }}
    >
      <Animated.View
        style={[styles.cover, { width, height: width * 1.5, transform: [{ translateY }] }]}
      >
        {cover ? (
          <Image source={{ uri: cover }} style={styles.image} resizeMode="cover" />
        ) : (
          <View style={[styles.image, { backgroundColor: spineColor(book.id) }]}>
            <View pointerEvents="none" style={styles.coverWash} />
            <View style={styles.titledInner}>
              <Text style={styles.titledTitle} numberOfLines={4}>
                {book.title}
              </Text>
              {book.author ? (
                <Text style={styles.titledAuthor} numberOfLines={1}>
                  {book.author.toUpperCase()}
                </Text>
              ) : null}
            </View>
          </View>
        )}

        {/* Bound spine edge + a thin page seam + a diagonal sheen. */}
        <View pointerEvents="none" style={styles.spineShade} />
        <View pointerEvents="none" style={styles.spineSeam} />
        <View pointerEvents="none" style={styles.sheen} />

        {!ready ? (
          <>
            <View pointerEvents="none" style={styles.importScrim} />
            {working && pct > 0 ? (
              <View pointerEvents="none" style={styles.progressTrack}>
                <View style={[styles.progressFill, { width: `${pct}%` }]} />
              </View>
            ) : null}
            <View style={styles.statusBar}>
              <Text style={styles.statusText} numberOfLines={1}>
                {working && pct > 0 ? `${stageLabel(book).toUpperCase()} · ${pct}%` : stageLabel(book).toUpperCase()}
              </Text>
            </View>
          </>
        ) : null}
      </Animated.View>

      {/* Contact shadow on the plank. */}
      <View style={styles.contact} />

      <Text style={styles.caption} numberOfLines={1}>
        {book.title}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  cover: {
    borderTopLeftRadius: 3,
    borderTopRightRadius: 7,
    borderBottomRightRadius: 7,
    borderBottomLeftRadius: 3,
    overflow: "hidden",
    ...Platform.select({
      ios: {
        shadowColor: "#000",
        shadowOffset: { width: 0, height: 14 },
        shadowOpacity: 0.55,
        shadowRadius: 20,
      },
      android: { elevation: 10 },
      default: {},
    }),
  },
  image: { width: "100%", height: "100%" },
  coverWash: { ...StyleSheet.absoluteFill, backgroundColor: "rgba(0,0,0,0.28)" },
  titledInner: { flex: 1, justifyContent: "space-between", padding: 12 },
  titledTitle: {
    fontFamily: fonts.display,
    color: alpha.white95,
    fontSize: 15,
    lineHeight: 19,
    fontWeight: "600",
  },
  titledAuthor: {
    color: alpha.white55,
    fontSize: type.micro.fontSize,
    letterSpacing: 1.4,
  },
  spineShade: { position: "absolute", top: 0, bottom: 0, left: 0, width: 7, backgroundColor: "rgba(0,0,0,0.40)" },
  spineSeam: { position: "absolute", top: 0, bottom: 0, left: 7, width: StyleSheet.hairlineWidth, backgroundColor: alpha.white12 },
  sheen: { ...StyleSheet.absoluteFill, backgroundColor: "rgba(255,255,255,0.05)" },
  importScrim: { ...StyleSheet.absoluteFill, backgroundColor: "rgba(0,0,0,0.35)" },
  progressTrack: {
    position: "absolute",
    left: 10,
    right: 10,
    bottom: 34,
    height: 4,
    borderRadius: 999,
    backgroundColor: "rgba(0,0,0,0.45)",
    overflow: "hidden",
  },
  progressFill: {
    height: "100%",
    borderRadius: 999,
    backgroundColor: palette.emberGlow,
  },
  statusBar: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    paddingVertical: 4,
    paddingHorizontal: 8,
    backgroundColor: "rgba(0,0,0,0.62)",
    alignItems: "center",
  },
  statusText: {
    color: palette.emberGlow,
    fontSize: type.micro.fontSize,
    fontWeight: "600",
    letterSpacing: 1,
  },
  contact: {
    height: 8,
    marginTop: 5,
    marginHorizontal: "8%",
    borderRadius: 999,
    backgroundColor: "rgba(0,0,0,0.45)",
    opacity: 0.8,
  },
  caption: {
    marginTop: 8,
    color: alpha.white72,
    fontSize: type.caption.fontSize,
    textAlign: "center",
  },
});
