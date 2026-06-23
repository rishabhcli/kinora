import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { ScrollView, StyleSheet, Text } from "react-native";

import { api } from "../lib/api";

interface WordBox {
  word_index?: number;
  text?: string;
}

/**
 * On phones we don't render the photographed page — we reflow the words (the
 * sync is word-level) and paint the karaoke highlight on the active word.
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
  const { data } = useQuery({
    queryKey: queryKeys.page(bookId, page),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: bookId, page_number: page } },
      });
      if (error || !data) throw new Error("failed to load page");
      return data;
    },
  });

  const words = (data?.word_boxes ?? []) as WordBox[];

  return (
    <ScrollView style={styles.scroll} contentContainerStyle={styles.content}>
      {words.length > 0 ? (
        <Text style={styles.body}>
          {words.map((word, index) => {
            const active = word.word_index !== undefined && word.word_index === highlightWordIndex;
            return (
              <Text key={word.word_index ?? index} style={active ? styles.active : undefined}>
                {word.text ?? ""}{" "}
              </Text>
            );
          })}
        </Text>
      ) : (
        <Text style={styles.body}>{data?.text ?? "Loading…"}</Text>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  scroll: { flex: 1, backgroundColor: "#0a0a0a" },
  content: { padding: 20 },
  body: { color: "#d4d4d4", fontSize: 18, lineHeight: 30 },
  active: { backgroundColor: "#fcd34d", color: "#0a0a0a" },
});
