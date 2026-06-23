import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { ActivityIndicator, FlatList, Pressable, StyleSheet, Text, View } from "react-native";

import { useAuth } from "../hooks/useAuth";
import { api } from "../lib/api";
import { authStore, persistToken } from "../lib/auth";

export function ShelfScreen({ onOpen }: { onOpen: (bookId: string) => void }) {
  const email = useAuth((state) => state.user?.email);

  const { data: books, isLoading } = useQuery({
    queryKey: queryKeys.books(),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books");
      if (error || !data) throw new Error("failed to load books");
      return data;
    },
  });

  function signOut() {
    persistToken(null);
    authStore.getState().setAnonymous();
  }

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.title}>Library</Text>
        <Pressable onPress={signOut}>
          <Text style={styles.signOut}>{email ? "Sign out" : "Sign out"}</Text>
        </Pressable>
      </View>
      {isLoading ? (
        <ActivityIndicator color="#fff" style={{ marginTop: 24 }} />
      ) : (
        <FlatList
          data={books ?? []}
          keyExtractor={(book) => book.id}
          contentContainerStyle={styles.list}
          renderItem={({ item }) => (
            <Pressable style={styles.card} onPress={() => onOpen(item.id)}>
              <Text style={styles.cardTitle} numberOfLines={1}>
                {item.title}
              </Text>
              {item.author ? (
                <Text style={styles.cardAuthor} numberOfLines={1}>
                  {item.author}
                </Text>
              ) : null}
              <Text style={styles.cardStatus}>{item.status}</Text>
            </Pressable>
          )}
          ListEmptyComponent={
            <Text style={styles.empty}>No books yet. Upload a PDF on desktop.</Text>
          }
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#0a0a0a", paddingTop: 60 },
  header: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingHorizontal: 20,
    paddingBottom: 16,
  },
  title: { color: "#fff", fontSize: 18, fontWeight: "600" },
  signOut: { color: "#a3a3a3" },
  list: { padding: 20, gap: 12 },
  card: {
    backgroundColor: "#171717",
    borderRadius: 8,
    padding: 16,
    borderWidth: 1,
    borderColor: "#262626",
  },
  cardTitle: { color: "#fff", fontSize: 14, fontWeight: "500" },
  cardAuthor: { color: "#737373", fontSize: 12, marginTop: 2 },
  cardStatus: { color: "#737373", fontSize: 11, marginTop: 6, textTransform: "uppercase" },
  empty: { color: "#a3a3a3", paddingHorizontal: 20 },
});
