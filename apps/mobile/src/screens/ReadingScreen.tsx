import {
  type BeatStage,
  conflictResolution,
  queryKeys,
  selectActiveConflict,
  type SocketStatus,
} from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEvent, useEventListener } from "expo";
import { useVideoPlayer, VideoView } from "expo-video";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Pressable,
  StyleSheet,
  Text,
  useWindowDimensions,
  View,
} from "react-native";

import { AgentActivityFeed } from "../components/AgentActivityFeed";
import { BufferIndicator } from "../components/BufferIndicator";
import { ConflictSheet } from "../components/ConflictSheet";
import { DegradedStage, type DegradedVariant } from "../components/DegradedStage";
import { PdfPageView } from "../components/PdfPageView";
import { ReflowReader } from "../components/ReflowReader";
import { SegmentedControl } from "../components/ui";
import { CanonSheet } from "./CanonSheet";
import { MetricsSheet } from "./MetricsSheet";
import { usePreferences } from "../hooks/usePreferences";
import { useSyncEngine } from "../hooks/useSyncEngine";
import { api } from "../lib/api";
import { alpha, BOTTOM_INSET, palette, space, TABLET_BREAKPOINT, TOP_INSET, type } from "../theme/tokens";

type ReadingView = "read" | "watch";

/**
 * The mobile reading room. On a phone the desktop's left-text / right-video split
 * doesn't fit, so we use a Read / Watch segmented control: "Read" docks a compact
 * film strip above the scrolling read-along; "Watch" expands the film to fill.
 * On a tablet (or landscape) we fall back to the side-by-side split.
 *
 * The film implements the §12.4 ladder: when the committed clip isn't on screen
 * yet it bridges with a {@link DegradedStage} (a Ken-Burns'd keyframe / page
 * illustration, or the audio-text floor) rather than a spinner, and incoming
 * clips are double-buffered across two `useVideoPlayer` instances — the new
 * source preloads into the idle player and the VideoView flips to it once it
 * reports `readyToPlay` (a clean boundary), never mutating the visible source.
 */
export function ReadingScreen({ bookId, onBack }: { bookId: string; onBack: () => void }) {
  const { width, height } = useWindowDimensions();
  const isWide = width >= TABLET_BREAKPOINT || width > height;
  const [view, setView] = useState<ReadingView>("read");
  const [feedOpen, setFeedOpen] = useState(false);
  const [canonOpen, setCanonOpen] = useState(false);
  const [metricsOpen, setMetricsOpen] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const autoplay = usePreferences((state) => state.autoplayOnOpen);

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

  const { engine, snapshot, activity, socketStatus, resolveConflict } = useSyncEngine(sessionId);

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

  // The §7.2 dispute the Crew-dispute sheet should show + its streamed resolution.
  const [dismissedConflicts, setDismissedConflicts] = useState<ReadonlySet<string>>(new Set());
  const dismissConflict = useCallback((conflictId: string) => {
    setDismissedConflicts((prev) => new Set(prev).add(conflictId));
  }, []);
  // The feed's "Resolve…" CTA un-dismisses a dispute so the sheet re-opens (§7.2).
  const reopenConflict = useCallback((conflictId: string) => {
    setDismissedConflicts((prev) => {
      if (!prev.has(conflictId)) return prev;
      const next = new Set(prev);
      next.delete(conflictId);
      return next;
    });
  }, []);
  // A regen entry links to its shot: seek there + reveal the film behind the sheet.
  const onSelectShot = useCallback(
    (shotId: string) => {
      const span = shots?.find((sh) => sh.shot_id === shotId)?.source_span as
        | { word_range?: [number, number] }
        | null
        | undefined;
      const startWord = span?.word_range?.[0];
      if (typeof startWord === "number") engine.seek(startWord, Date.now());
      setFeedOpen(false);
    },
    [shots, engine],
  );
  // Unread crew entries since the feed was last opened (badge on the header).
  const seenIdRef = useRef(-1);
  const [unread, setUnread] = useState(0);
  useEffect(() => {
    const newest = activity[0]?.id ?? -1;
    if (newest < seenIdRef.current) seenIdRef.current = -1;
    if (feedOpen) {
      seenIdRef.current = newest;
      setUnread(0);
    } else {
      setUnread(activity.filter((a) => a.id > seenIdRef.current).length);
    }
  }, [activity, feedOpen]);
  const activeConflict = useMemo(
    () => selectActiveConflict(activity, dismissedConflicts),
    [activity, dismissedConflicts],
  );
  const conflictTrace = useMemo(
    () => conflictResolution(activity, activeConflict),
    [activity, activeConflict],
  );
  const disputedClipUrl = useMemo(() => {
    if (!activeConflict?.shotId) return null;
    return shots?.find((s) => s.shot_id === activeConflict.shotId)?.clip_url ?? null;
  }, [shots, activeConflict]);

  // Feed the book's own page image into the ladder as the illustration rung
  // (§12.4) — the deep fallback the film pans when no keyframe exists for a beat.
  const { data: pageData } = useQuery({
    queryKey: queryKeys.page(bookId, snapshot.currentPage),
    enabled: snapshot.currentPage > 0,
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: bookId, page_number: snapshot.currentPage } },
      });
      if (error || !data) throw new Error("failed to load page");
      return data;
    },
  });
  useEffect(() => {
    if (pageData?.image_url) engine.setPageIllustration(snapshot.currentPage, pageData.image_url);
  }, [pageData, snapshot.currentPage, engine]);

  // --- Double-buffered playback (§5.2): two players, preload the idle one. --- #
  const clipUrl = snapshot.currentClipUrl;
  const playerA = useVideoPlayer(null, (p) => {
    p.timeUpdateEventInterval = 0.25;
  });
  const playerB = useVideoPlayer(null, (p) => {
    p.timeUpdateEventInterval = 0.25;
  });
  const [activeBuf, setActiveBuf] = useState<"A" | "B">("A");
  const [activeUrl, setActiveUrl] = useState<string | null>(null);
  const pendingRef = useRef<{ buf: "A" | "B"; url: string } | null>(null);
  const activePlayer = activeBuf === "A" ? playerA : playerB;

  // §4.8 deliberate-seek signal from the engine (mid-scene seek / scene hot-swap),
  // read at promote time without re-creating the callback; `applied` dedups it.
  const seekRef = useRef<{ s: number | null; seq: number }>({ s: null, seq: 0 });
  seekRef.current = { s: snapshot.playheadSeekS, seq: snapshot.playheadSeekSeq };
  const appliedSeekSeqRef = useRef(0);
  // The source each player currently holds, so a proactively warmed nextSource
  // (preloaded into the idle player ahead of the boundary) is promoted without a
  // re-fetch — the boundary swap is then instant.
  const loadedRef = useRef<{ A: string | null; B: string | null }>({ A: null, B: null });

  // Promote the freshly-loaded idle player to visible + reporting, retiring the
  // other so only one ever plays.
  const promote = useCallback(
    (buf: "A" | "B", url: string) => {
      const incoming = buf === "A" ? playerA : playerB;
      const other = buf === "A" ? playerB : playerA;
      // Land a deliberate seek / scene hot-swap on the word's frame (§4.8/§9.6) so
      // swapping per-shot playback for the stitched scene continues from the same
      // moment; a natural boundary change carries no pending seek → start at 0.
      const { s, seq } = seekRef.current;
      const seekPending = seq !== appliedSeekSeqRef.current && s != null;
      if (seekPending) appliedSeekSeqRef.current = seq;
      incoming.currentTime = seekPending && s != null && s > 0 ? s : 0;
      if (autoplay) incoming.play();
      other.pause();
      setActiveBuf(buf);
      setActiveUrl(url);
    },
    [playerA, playerB, autoplay],
  );

  const onStatus = useCallback(
    (buf: "A" | "B", status: string) => {
      const pending = pendingRef.current;
      if (pending && pending.buf === buf && status === "readyToPlay") {
        pendingRef.current = null;
        promote(buf, pending.url);
        return;
      }
      if (status === "error") {
        // The source URL went dead (expired presigned URL / network) — drop it so
        // the engine re-resolves to the next-best rung, and forget the dead buffer.
        const url = loadedRef.current[buf];
        loadedRef.current[buf] = null;
        if (pendingRef.current?.buf === buf) pendingRef.current = null;
        if (!url) return;
        const snap = engine.getSnapshot();
        if (url === snap.currentClipUrl && snap.currentSource) {
          engine.markSourceFailed(snap.currentSource.id);
        } else if (snap.nextSource && url === snap.nextSource.url) {
          engine.markSourceFailed(snap.nextSource.id);
        }
      }
    },
    [promote, engine],
  );

  useEventListener(playerA, "statusChange", (e) => onStatus("A", e.status));
  useEventListener(playerB, "statusChange", (e) => onStatus("B", e.status));

  // The active asset played to its end → flow continuously into the preloaded
  // next source (§9.6); a no-op when nothing is queued, so it just stops.
  useEventListener(playerA, "playToEnd", () => {
    if (activeBuf === "A") engine.advanceToNextSource();
  });
  useEventListener(playerB, "playToEnd", () => {
    if (activeBuf === "B") engine.advanceToNextSource();
  });

  // When the committed source changes, preload it into the idle buffer; the swap
  // happens once it reports readyToPlay (above), so the visible film never blanks.
  useEffect(() => {
    if (!clipUrl || clipUrl === activeUrl) return;
    const buf = activeBuf === "A" ? "B" : "A";
    const incoming = buf === "A" ? playerA : playerB;
    // Warmed by the proactive preload below → promote without a re-fetch (the
    // boundary swap is instant). Otherwise load it now and swap on readyToPlay.
    if (loadedRef.current[buf] === clipUrl) {
      if (incoming.status === "readyToPlay") promote(buf, clipUrl);
      else pendingRef.current = { buf, url: clipUrl };
      return;
    }
    pendingRef.current = { buf, url: clipUrl };
    loadedRef.current[buf] = clipUrl;
    let cancelled = false;
    void incoming
      .replaceAsync(clipUrl)
      .then(() => {
        // If it loaded instantly, statusChange may not refire — promote directly.
        if (!cancelled && incoming.status === "readyToPlay") onStatus(buf, "readyToPlay");
      })
      .catch(() => {
        if (pendingRef.current?.url === clipUrl) pendingRef.current = null;
        if (loadedRef.current[buf] === clipUrl) loadedRef.current[buf] = null;
      });
    return () => {
      cancelled = true;
    };
  }, [clipUrl, activeUrl, activeBuf, playerA, playerB, onStatus, promote]);

  // Proactively warm the idle player with nextSource (§5.2/§9.6) so the boundary
  // swap is instant. Only while stably playing, so it never disturbs a live swap.
  useEffect(() => {
    const url = snapshot.nextSource?.url;
    if (!url || url === clipUrl || clipUrl !== activeUrl) return;
    const idleBuf = activeBuf === "A" ? "B" : "A";
    if (loadedRef.current[idleBuf] === url) return;
    const idle = idleBuf === "A" ? playerA : playerB;
    loadedRef.current[idleBuf] = url;
    void idle.replaceAsync(url).catch(() => {
      if (loadedRef.current[idleBuf] === url) loadedRef.current[idleBuf] = null;
    });
  }, [snapshot.nextSource?.url, clipUrl, activeUrl, activeBuf, playerA, playerB]);

  // Step off video onto the degraded rung quietly (reader reached an uncommitted
  // beat) — pause both players rather than play on under the bridge.
  useEffect(() => {
    if (snapshot.currentStage !== "full_video") {
      playerA.pause();
      playerB.pause();
    }
  }, [snapshot.currentStage, playerA, playerB]);

  // Drive the read-along from whichever player is active (the other is paused).
  const tuA = useEvent(playerA, "timeUpdate");
  const tuB = useEvent(playerB, "timeUpdate");
  useEffect(() => {
    const tu = activeBuf === "A" ? tuA : tuB;
    if (tu) engine.reportVideoTime(tu.currentTime, Date.now());
  }, [activeBuf, tuA, tuB, engine]);

  // A deliberate seek within the *same* source (§4.8): jump the active player in
  // place. (A seek that also swaps the source applies the seek in `promote`.)
  useEffect(() => {
    if (snapshot.playheadSeekSeq === appliedSeekSeqRef.current) return;
    if (clipUrl !== activeUrl) return; // a source swap is in flight — it will seek
    appliedSeekSeqRef.current = snapshot.playheadSeekSeq;
    if (snapshot.playheadSeekS != null) activePlayer.currentTime = snapshot.playheadSeekS;
  }, [snapshot.playheadSeekSeq, snapshot.playheadSeekS, clipUrl, activeUrl, activePlayer]);

  // The committed clip for this beat isn't on the visible buffer yet → bridge.
  const showBridge = snapshot.currentStage !== "full_video" || activeUrl !== clipUrl;
  const bridgeStill = snapshot.currentKeyframeUrl ?? snapshot.currentIllustrationUrl;
  const bridgeVariant: DegradedVariant = snapshot.currentKeyframeUrl
    ? "keyframe"
    : snapshot.currentIllustrationUrl
      ? "illustration"
      : "audio_text";

  const reader = isWide ? (
    <PdfPageView
      bookId={bookId}
      page={snapshot.currentPage}
      highlightWordIndex={snapshot.highlightWordIndex}
      onSeekWord={(word) => engine.seek(word, Date.now())}
    />
  ) : (
    <ReflowReader
      bookId={bookId}
      page={snapshot.currentPage}
      highlightWordIndex={snapshot.highlightWordIndex}
    />
  );

  const film = (
    <FilmPanel
      player={activePlayer}
      showBridge={showBridge}
      bridgeStill={bridgeStill}
      bridgeVariant={bridgeVariant}
      beatId={snapshot.currentBeatId}
      budgetRemaining={snapshot.budgetRemaining}
      underBudgetPressure={snapshot.underBudgetPressure}
      sessionId={sessionId}
      focusWord={snapshot.focusWord}
      velocity={snapshot.velocity}
      committedAheadS={snapshot.committedSecondsAhead ?? 0}
      stage={snapshot.currentStage}
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
        socketStatus={socketStatus}
        unread={unread}
        onOpenFeed={() => setFeedOpen(true)}
        onOpenCanon={() => setCanonOpen(true)}
        onOpenMetrics={() => setMetricsOpen(true)}
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

      <ConflictSheet
        conflict={activeConflict}
        trace={conflictTrace}
        shotClipUrl={disputedClipUrl}
        onResolve={resolveConflict}
        onDismiss={dismissConflict}
      />

      <CanonSheet
        visible={canonOpen}
        bookId={bookId}
        shots={shots}
        activity={activity}
        onClose={() => setCanonOpen(false)}
      />

      <MetricsSheet visible={metricsOpen} bookId={bookId} onClose={() => setMetricsOpen(false)} />ic

      <AgentActivityFeed
        activity={activity}
        socketStatus={socketStatus}
        visible={feedOpen}
        onClose={() => setFeedOpen(false)}
        onSelectShot={onSelectShot}
        onResolveConflict={(c) => reopenConflict(c.conflictId)}
      />
    </View>
  );
}

/** The reading-room top bar: back to the library + a Crew-activity toggle and a
 *  quiet live read-out. */
function Header({
  onBack,
  velocity,
  owner,
  page,
  socketStatus,
  unread,
  onOpenFeed,
  onOpenCanon,
}: {
  onBack: () => void;
  velocity: number;
  owner: string;
  page: number;
  socketStatus: SocketStatus;
  unread: number;
  onOpenFeed: () => void;
  onOpenCanon: () => void;
}) {
  const dotColor =
    socketStatus === "open" ? "#86efac" : socketStatus === "connecting" ? "#fcd34d" : alpha.white40;
  return (
    <View style={styles.header}>
      <Pressable onPress={onBack} hitSlop={10} accessibilityRole="button" style={styles.backBtn}>
        <Text style={styles.backChevron}>‹</Text>
        <Text style={styles.backLabel}>Library</Text>
      </Pressable>
      <View style={styles.headerRight}>
        <Pressable
          onPress={onOpenCanon}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel="Canon editor"
          style={styles.crewBtn}
        >
          <Text style={styles.crewLabel}>Canon</Text>
        </Pressable>
        <Pressable
          onPress={onOpenFeed}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel="Crew activity"
          style={styles.crewBtn}
        >
          <View style={[styles.crewDot, { backgroundColor: dotColor }]} />
          <Text style={styles.crewLabel}>Crew</Text>
          {unread > 0 ? (
            <View style={styles.unreadBadge}>
              <Text style={styles.unreadText}>{unread > 99 ? "99+" : unread}</Text>
            </View>
          ) : null}
        </Pressable>
        <View style={styles.meta}>
          <Text style={styles.metaPrimary}>Page {page > 0 ? page : "—"}</Text>
          <Text style={styles.metaSecondary}>
            {owner === "video" ? "Playing" : "Reading"} · {velocity.toFixed(1)} wps
          </Text>
        </View>
      </View>
    </View>
  );
}

/** The film surface: expo-video when a committed clip is on screen, else the
 *  §12.4 degraded bridge (Ken-Burns still / audio-text floor). */
function FilmPanel({
  player,
  showBridge,
  bridgeStill,
  bridgeVariant,
  beatId,
  budgetRemaining,
  underBudgetPressure,
  sessionId,
  focusWord,
  velocity,
  committedAheadS,
  stage,
  expanded,
  onExpand,
}: {
  player: ReturnType<typeof useVideoPlayer>;
  showBridge: boolean;
  bridgeStill: string | null;
  bridgeVariant: DegradedVariant;
  beatId: string | null;
  budgetRemaining: number | null;
  underBudgetPressure: boolean;
  sessionId: string | null;
  focusWord: number;
  velocity: number;
  committedAheadS: number;
  stage: BeatStage;
  expanded: boolean;
  /** Tap-to-expand handler; only honoured while the strip is docked (compact). */
  onExpand?: () => void;
}) {
  const body = showBridge ? (
    <DegradedStage
      stillUrl={bridgeStill}
      variant={bridgeVariant}
      seed={beatId}
      budgetRemaining={budgetRemaining}
      underBudgetPressure={underBudgetPressure}
    />
  ) : (
    <VideoView
      style={StyleSheet.absoluteFill}
      player={player}
      contentFit="contain"
      nativeControls={expanded}
    />
  );

  // The §5.3 buffer hairline + zone badge, overlaid on the film's top edge.
  const indicator = (
    <BufferIndicator
      sessionId={sessionId}
      focusWord={focusWord}
      velocity={velocity}
      committedAheadS={committedAheadS}
      stage={stage}
      budgetLow={underBudgetPressure}
    />
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
        {indicator}
      </Pressable>
    );
  }
  // Expanded / split: native controls drive playback, no overlay press target.
  return (
    <View style={[styles.film, styles.filmFill]}>
      {body}
      {indicator}
    </View>
  );
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

  segmentBar: { paddingHorizontal: space.xl, paddingTop: space.sm, paddingBottom: BOTTOM_INSET + space.sm },

  headerRight: { flexDirection: "row", alignItems: "center", gap: space.md },
  crewBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: space.md,
    paddingVertical: 6,
    borderRadius: 999,
    backgroundColor: alpha.white08,
  },
  crewDot: { width: 7, height: 7, borderRadius: 3.5 },
  crewLabel: { color: alpha.white72, fontSize: type.caption.fontSize, fontWeight: "600" },
  unreadBadge: {
    minWidth: 16,
    height: 16,
    borderRadius: 8,
    paddingHorizontal: 4,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: palette.ember,
  },
  unreadText: { color: palette.walnutDeep, fontSize: 10, fontWeight: "700", fontVariant: ["tabular-nums"] },
});
