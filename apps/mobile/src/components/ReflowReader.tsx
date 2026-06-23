import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";

import { useReducedMotion } from "../hooks/useReducedMotion";
import { api } from "../lib/api";
import { fonts, palette, space, type } from "../theme/tokens";

interface WordBox {
  word_index?: number;
  text?: string;
}

/**
 * The phone read-along. We don't render the photographed page here — the sync is
 * word-level, so we reflow the words onto a warm "parchment" column and paint a
 * soft ember karaoke highlight on the active word, gently keeping it in view.
 */
export function ReflowReader({
  bookId,
  page,
  highlightWordIndex,
}: {
  bookId: string;
  page: number;
  highlightWordIndex: number | null;
}) {
  const reduced = useReducedMotion();
  const scrollRef = useRef<ScrollView>(null);
  const activeYRef = useRef<number | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: queryKeys.page(bookId, page),
    enabled: page > 0,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: bookId, page_number: page } },
      });
      if (error || !data) throw new Error("failed to load page");
      return data;
    },
  });

  const words = (data?.word_boxes ?? []) as WordBox[];

  // Keep the active word comfortably in view as it advances.
  useEffect(() => {
    if (activeYRef.current == null) return;
    scrollRef.current?.scrollTo({
      y: Math.max(0, activeYRef.current - 160),
      animated: !reduced,
    });
  }, [highlightWordIndex, reduced]);

  return (
    <ScrollView
      ref={scrollRef}
      style={styles.scroll}
      contentContainerStyle={styles.content}
      showsVerticalScrollIndicator={false}
    >
      <View style={styles.leaf}>
        {isLoading ? (
          <Text style={styles.placeholder}>Turning to the page…</Text>
        ) : words.length > 0 ? (
          <Text style={styles.body}>
            {words.map((word, index) => {
              const active = word.word_index !== undefined && word.word_index === highlightWordIndex;
              return (
                <Text
                  key={word.word_index ?? index}
                  onLayout={
                    active
                      ? (e) => {
                          activeYRef.current = e.nativeEvent.layout.y;
                        }
                      : undefined
                  }
                  style={active ? styles.active : undefined}
                >
                  {word.text ?? ""}{" "}
                </Text>
              );
            })}
          </Text>
        ) : (
          <Text style={styles.body}>{data?.text ?? "This page has no text yet."}</Text>
        )}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  scroll: { flex: 1 },
  content: { padding: space.xl, paddingBottom: space.huge },
  leaf: {
    backgroundColor: palette.parchment,
    borderRadius: 14,
    padding: space.xxl,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: "rgba(0,0,0,0.10)",
  },
  body: {
    fontFamily: fonts.display,
    color: palette.ink,
    fontSize: type.reading.fontSize,
    lineHeight: type.reading.lineHeight,
  },
  active: {
    backgroundColor: palette.emberGlow,
    color: palette.walnutDeep,
    borderRadius: 4,
  },
  placeholder: {
    color: palette.inkSoft,
    fontSize: type.label.fontSize,
    fontStyle: "italic",
  },
});
