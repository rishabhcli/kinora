import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEvent } from "expo";
import { useVideoPlayer, VideoView } from "expo-video";
import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { ReflowReader } from "../components/ReflowReader";
import { useSyncEngine } from "../hooks/useSyncEngine";
import { api } from "../lib/api";

/** The mobile reading room: video pinned on top, reflowed read-along beneath. */
export function ReadingScreen({ bookId, onBack }: { bookId: string; onBack: () => void }) {
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

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Pressable onPress={onBack}>
          <Text style={styles.back}>← Library</Text>
        </Pressable>
        <Text style={styles.meta}>
          {snapshot.owner} · {snapshot.velocity.toFixed(1)} wps
        </Text>
      </View>
      <VideoView style={styles.video} player={player} contentFit="contain" nativeControls />
      <ReflowReader
        bookId={bookId}
        page={snapshot.currentPage}
        highlightWordIndex={snapshot.highlightWordIndex}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#0a0a0a", paddingTop: 50 },
  header: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingHorizontal: 16,
    paddingBottom: 8,
  },
  back: { color: "#a3a3a3" },
  meta: { color: "#737373", fontSize: 12 },
  video: { width: "100%", aspectRatio: 16 / 9, backgroundColor: "#000" },
});
