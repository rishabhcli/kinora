import { type BookResponse, queryKeys } from "@kinora/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  Image,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  useWindowDimensions,
  View,
} from "react-native";

import {
  AmbientBackdrop,
  BookCard,
  SearchField,
  Surface,
} from "../components/ui";
import { useAuth } from "../hooks/useAuth";
import { api } from "../lib/api";
import { authStore, persistToken } from "../lib/auth";
import { alpha, BOTTOM_INSET, fonts, HIT_TARGET, palette, radius, space, TABLET_BREAKPOINT, TOP_INSET, type } from "../theme/tokens";
import { SettingsSheet } from "./SettingsSheet";

const GRID_GAP = 18;
const SCREEN_PADDING = 20;

/** Chunk a flat list of books into shelf rows of `perRow`. */
function intoRows<T>(items: T[], perRow: number): T[][] {
  const rows: T[][] = [];
  for (let i = 0; i < items.length; i += perRow) rows.push(items.slice(i, i + perRow));
  return rows;
}

/** A warm oak shelf rail with a lit top edge and a shadowed front face. */
function ShelfRail() {
  return (
    <View style={railStyles.rail}>
      <View style={railStyles.lit} />
      <View style={railStyles.face} />
    </View>
  );
}

/**
 * The library: a warm bookshelf of covers over the screening-room backdrop.
 * Books still come from `GET /api/books`; tapping one opens it. The grid is
 * responsive — 2 covers per shelf on a phone, more on a tablet.
 */
export function ShelfScreen({ onOpen }: { onOpen: (bookId: string) => void }) {
  const email = useAuth((state) => state.user?.email);
  const { width } = useWindowDimensions();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [showSettings, setShowSettings] = useState(false);

  const isTablet = width >= TABLET_BREAKPOINT;
  const perRow = isTablet ? Math.min(5, Math.max(3, Math.floor((width - SCREEN_PADDING * 2) / 190))) : 2;
  const innerWidth = Math.min(width, 1040) - SCREEN_PADDING * 2;
  const cardWidth = Math.floor((innerWidth - GRID_GAP * (perRow - 1)) / perRow);

  const { data: books, isLoading } = useQuery({
    queryKey: queryKeys.books(),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books");
      if (error || !data) throw new Error("failed to load books");
      return data;
    },
  });

  // Cover warming: once the library resolves, fetch each ready book's page-1
  // (into the same React Query cache the BookCard reads, so the cover is an
  // instant cache hit) and prime the native image cache via Image.prefetch, so
  // covers paint immediately instead of fading in per-card on scroll.
  useEffect(() => {
    if (!books) return;
    let cancelled = false;
    for (const book of books) {
      if (book.status !== "ready") continue;
      void queryClient
        .fetchQuery({
          queryKey: queryKeys.page(book.id, 1),
          staleTime: 5 * 60 * 1000,
          queryFn: async () => {
            const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
              params: { path: { book_id: book.id, page_number: 1 } },
            });
            return error || !data ? null : data;
          },
        })
        .then((page) => {
          if (!cancelled && page?.image_url) void Image.prefetch(page.image_url);
        })
        .catch(() => {
          // Best-effort warming; the BookCard still fetches its own cover.
        });
    }
    return () => {
      cancelled = true;
    };
  }, [books, queryClient]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (books ?? []).filter(
      (b) => !q || b.title.toLowerCase().includes(q) || (b.author ?? "").toLowerCase().includes(q),
    );
  }, [books, query]);

  const rows: BookResponse[][] = useMemo(() => intoRows(filtered, perRow), [filtered, perRow]);

  function signOut() {
    persistToken(null);
    authStore.getState().setAnonymous();
  }

  const empty = !isLoading && filtered.length === 0;

  return (
    <AmbientBackdrop>
      <View style={styles.header}>
        <View style={styles.headerText}>
          <Text style={styles.eyebrow}>Your library</Text>
          <Text style={styles.title}>The shelf</Text>
        </View>
        <Pressable
          onPress={() => setShowSettings(true)}
          accessibilityRole="button"
          accessibilityLabel="Profile and settings"
          hitSlop={8}
          style={({ pressed }) => [styles.profileBtn, pressed && styles.profilePressed]}
        >
          <Text style={styles.profileGlyph}>{(email?.[0] ?? "K").toUpperCase()}</Text>
        </Pressable>
      </View>

      <View style={styles.searchRow}>
        <SearchField value={query} onChangeText={setQuery} />
      </View>

      <ScrollView
        contentContainerStyle={[styles.scroll, { maxWidth: 1040, alignSelf: "center", width: "100%" }]}
        showsVerticalScrollIndicator={false}
      >
        {isLoading ? (
          <View style={styles.loading}>
            <ActivityIndicator color={palette.emberGlow} />
            <Text style={styles.loadingText}>Opening your library…</Text>
          </View>
        ) : empty ? (
          <Surface style={styles.emptyCard}>
            <Text style={styles.emptyTitle}>
              {query ? "Nothing matches that" : "Your shelves are bare"}
            </Text>
            <Text style={styles.emptyBody}>
              {query
                ? "Try a different title or author."
                : "Add a PDF on desktop and Kinora will begin the film. It will appear here automatically."}
            </Text>
          </Surface>
        ) : (
          rows.map((row, i) => (
            <View key={i} style={styles.shelf}>
              <View style={[styles.row, { gap: GRID_GAP }]}>
                {row.map((book) => (
                  <BookCard
                    key={book.id}
                    book={book}
                    width={cardWidth}
                    onPress={() => onOpen(book.id)}
                  />
                ))}
              </View>
              <ShelfRail />
            </View>
          ))
        )}
      </ScrollView>

      <SettingsSheet
        visible={showSettings}
        onClose={() => setShowSettings(false)}
        onSignOut={() => {
          setShowSettings(false);
          signOut();
        }}
      />
    </AmbientBackdrop>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: SCREEN_PADDING,
    paddingTop: TOP_INSET,
    paddingBottom: space.md,
  },
  headerText: { gap: 2 },
  profileBtn: {
    width: HIT_TARGET,
    height: HIT_TARGET,
    borderRadius: radius.pill,
    backgroundColor: alpha.emberSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white16,
    alignItems: "center",
    justifyContent: "center",
  },
  profilePressed: { opacity: 0.7 },
  profileGlyph: {
    fontFamily: fonts.display,
    color: palette.emberGlow,
    fontSize: type.heading.fontSize,
    fontWeight: "600",
  },
  eyebrow: {
    color: palette.emberGlow,
    fontSize: type.micro.fontSize,
    letterSpacing: 1.6,
    textTransform: "uppercase",
  },
  title: {
    fontFamily: fonts.display,
    color: palette.parchment,
    fontSize: type.display.fontSize,
    lineHeight: type.display.lineHeight,
    fontWeight: "600",
  },
  searchRow: {
    paddingHorizontal: SCREEN_PADDING,
    paddingBottom: space.lg,
  },
  scroll: { paddingHorizontal: SCREEN_PADDING, paddingBottom: BOTTOM_INSET + space.huge },
  loading: { alignItems: "center", paddingTop: space.huge, gap: space.md },
  loadingText: { color: alpha.white55, fontSize: type.label.fontSize },
  shelf: { marginBottom: space.xxxl },
  row: { flexDirection: "row" },
  emptyCard: { marginTop: space.xxxl, padding: space.xxl, alignItems: "center" },
  emptyTitle: {
    fontFamily: fonts.display,
    color: palette.parchment,
    fontSize: type.title.fontSize,
    fontWeight: "600",
    textAlign: "center",
  },
  emptyBody: {
    color: alpha.white55,
    fontSize: type.label.fontSize,
    lineHeight: type.body.lineHeight,
    textAlign: "center",
    marginTop: 6,
  },
});

const railStyles = StyleSheet.create({
  rail: { marginTop: -2 },
  lit: {
    height: 22,
    borderTopLeftRadius: 4,
    borderTopRightRadius: 4,
    backgroundColor: palette.oak,
    borderTopWidth: 2,
    borderTopColor: "rgba(255,226,178,0.35)",
  },
  face: {
    height: 8,
    borderBottomLeftRadius: 7,
    borderBottomRightRadius: 7,
    backgroundColor: palette.oakDark,
  },
});
