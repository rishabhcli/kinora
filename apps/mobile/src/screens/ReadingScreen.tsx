import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEvent } from "expo";
import { useVideoPlayer, VideoView } from "expo-video";
import { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  useWindowDimensions,
  View,
} from "react-native";

import { ReflowReader } from "../components/ReflowReader";
import { SegmentedControl } from "../components/ui";
import { useSyncEngine } from "../hooks/useSyncEngine";
import { api } from "../lib/api";
import { alpha, palette, space, TABLET_BREAKPOINT, TOP_INSET, type } from "../theme/tokens";

type ReadingView = "read" | "watch";

/**
 * The mobile reading room. On a phone the desktop's left-text / right-video split
 * doesn't fit, so we use a Read / Watch segmented control: "Read" docks a compact
 * film strip above the scrolling read-along; "Watch" expands the film to fill.
 * On a tablet (or landscape) we fall back to the side-by-side split.
 *
 * The data layer is unchanged: a session is created, the SyncEngine drives the
 * playhead, and expo-video reports playback time back into it.
 */
export function ReadingScreen({ bookId, onBack }: { bookId: string; onBack: () => void }) {
  const { width, height } = useWindowDimensions();
  const isWide = width >= TABLET_BREAKPOINT || width > height;
  const [view, setView] = useState<ReadingView>("read");
  const [sessionId, setSessionId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api
      .POST("/api/sessions", { body: { book_id: bookId, focus_word: 0, mode: "viewer" } })
      .then(({ data }) => {
        if (!cancelled && data) setSessionId(data.session_id);
      });
    return () => {
      cancelled = true;
    };
  }, [bookId]);

  const { engine, snapshot } = useSyncEngine(sessionId);

  const { data: shots } = useQuery({
    queryKey: queryKeys.shots(bookId),
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/shots", {
        params: { path: { book_id: bookId } },
      });
      if (error || !data) throw new Error("failed to load shots");
      return data;
    },
  });

  useEffect(() => {
    if (shots) engine.setShots(shots);
  }, [shots, engine]);

  const player = useVideoPlayer(snapshot.currentClipUrl, (instance) => {
    instance.timeUpdateEventInterval = 0.25;
    instance.play();
  });

  useEffect(() => {
    if (snapshot.currentClipUrl) player.play();
  }, [snapshot.currentClipUrl, player]);

  const timeUpdate = useEvent(player, "timeUpdate");

  useEffect(() => {
    if (timeUpdate) engine.reportVideoTime(timeUpdate.currentTime, Date.now());
  }, [timeUpdate, engine]);

  const reader = (
    <ReflowReader
      bookId={bookId}
      page={snapshot.currentPage}
      highlightWordIndex={snapshot.highlightWordIndex}
    />
  );

  const film = (
    <FilmPanel
      player={player}
      hasClip={!!snapshot.currentClipUrl}
      expanded={isWide || view === "watch"}
      // Only the compact, docked strip is tap-to-expand; once expanded, native
      // playback controls take over and the segmented control flips back.
      onExpand={isWide ? undefined : () => setView("watch")}
    />
  );

  return (
    <View style={styles.container}>
      <Header
        onBack={onBack}
        velocity={snapshot.velocity}
        owner={snapshot.owner}
        page={snapshot.currentPage}
      />

      {isWide ? (
        // Tablet / landscape: side-by-side split (read left, watch right).
        <View style={styles.splitRow}>
          <View style={styles.splitReader}>{reader}</View>
          <View style={styles.splitFilm}>{film}</View>
        </View>
      ) : view === "watch" ? (
        // Phone, watching: the film fills the stage.
        <View style={styles.flex}>{film}</View>
      ) : (
        // Phone, reading: a docked film strip above the scrolling read-along.
        <View style={styles.flex}>
          <View style={styles.dockedFilm}>{film}</View>
          <View style={styles.flex}>{reader}</View>
        </View>
      )}

      {!isWide ? (
        <View style={styles.segmentBar}>
          <SegmentedControl<ReadingView>
            value={view}
            onChange={setView}
            options={[
              { value: "read", label: "Read" },
              { value: "watch", label: "Watch" },
            ]}
          />
        </View>
      ) : null}
    </View>
  );
}

/** The reading-room top bar: back to the library + a quiet live read-out. */
function Header({
  onBack,
  velocity,
  owner,
  page,
}: {
  onBack: () => void;
  velocity: number;
  owner: string;
  page: number;
}) {
  return (
    <View style={styles.header}>
      <Pressable onPress={onBack} hitSlop={10} accessibilityRole="button" style={styles.backBtn}>
        <Text style={styles.backChevron}>‹</Text>
        <Text style={styles.backLabel}>Library</Text>
      </Pressable>
      <View style={styles.meta}>
        <Text style={styles.metaPrimary}>Page {page > 0 ? page : "—"}</Text>
        <Text style={styles.metaSecondary}>
          {owner === "video" ? "Playing" : "Reading"} · {velocity.toFixed(1)} wps
        </Text>
      </View>
    </View>
  );
}

/** The film surface: expo-video when a clip is live, else a composed-ahead state. */
function FilmPanel({
  player,
  hasClip,
  expanded,
  onExpand,
}: {
  player: ReturnType<typeof useVideoPlayer>;
  hasClip: boolean;
  expanded: boolean;
  /** Tap-to-expand handler; only honoured while the strip is docked (compact). */
  onExpand?: () => void;
}) {
  const body = hasClip ? (
    <VideoView
      style={StyleSheet.absoluteFill}
      player={player}
      contentFit="contain"
      nativeControls={expanded}
    />
  ) : (
    <View style={styles.filmPlaceholder}>
      <ActivityIndicator color={palette.emberGlow} />
      <Text style={styles.filmPlaceholderText}>Composing the film a few seconds ahead…</Text>
    </View>
  );

  // Docked: a tappable strip that expands into the full film stage.
  if (!expanded && onExpand) {
    return (
      <Pressable
        onPress={onExpand}
        accessibilityRole="button"
        accessibilityLabel="Expand the film"
        style={[styles.film, styles.filmDocked]}
      >
        {body}
        <View pointerEvents="none" style={styles.tapHint} />
      </Pressable>
    );
  }
  // Expanded / split: native controls drive playback, no overlay press target.
  return <View style={[styles.film, styles.filmFill]}>{body}</View>;
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: palette.walnutDeep },
  flex: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: space.lg,
    paddingTop: TOP_INSET,
    paddingBottom: space.md,
  },
  backBtn: { flexDirection: "row", alignItems: "center", gap: 2 },
  backChevron: { color: palette.emberGlow, fontSize: 26, lineHeight: 26, marginTop: -2 },
  backLabel: { color: alpha.white72, fontSize: type.label.fontSize, fontWeight: "500" },
  meta: { alignItems: "flex-end" },
  metaPrimary: { color: palette.parchment, fontSize: type.label.fontSize, fontWeight: "600" },
  metaSecondary: { color: alpha.white40, fontSize: type.caption.fontSize, marginTop: 1 },

  dockedFilm: { paddingHorizontal: space.lg, paddingBottom: space.md },

  film: { backgroundColor: "#000", overflow: "hidden" },
  filmDocked: { width: "100%", aspectRatio: 16 / 9, borderRadius: 14 },
  filmFill: { flex: 1 },
  filmPlaceholder: { ...StyleSheet.absoluteFill, alignItems: "center", justifyContent: "center", gap: 10, padding: space.xl },
  filmPlaceholderText: { color: alpha.white55, fontSize: type.caption.fontSize, textAlign: "center" },
  tapHint: {
    position: "absolute",
    bottom: 0,
    left: 0,
    right: 0,
    height: 1.5,
    backgroundColor: alpha.emberSoft,
  },

  splitRow: { flex: 1, flexDirection: "row" },
  splitReader: { flex: 1 },
  splitFilm: { width: "45%", maxWidth: 560, backgroundColor: "#000" },

  segmentBar: { paddingHorizontal: space.xl, paddingTop: space.sm, paddingBottom: space.xl },
});
