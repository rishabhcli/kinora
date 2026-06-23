import { containRect, queryKeys, wordAtNormalisedPoint } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  Image,
  type LayoutChangeEvent,
  type NativeTouchEvent,
  PanResponder,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { api } from "../lib/api";
import { palette, space, type } from "../theme/tokens";

interface WordBox {
  word_index: number;
  bbox: [number, number, number, number];
}

/** Coerce the loosely-typed `word_boxes` JSON into validated boxes (§9.4). */
function parseWordBoxes(raw: ReadonlyArray<Record<string, unknown>> | null | undefined): WordBox[] {
  if (!raw) return [];
  const out: WordBox[] = [];
  for (const row of raw) {
    const wordIndex = row["word_index"];
    const bbox = row["bbox"];
    if (typeof wordIndex !== "number" || !Array.isArray(bbox) || bbox.length < 4) continue;
    out.push({
      word_index: wordIndex,
      bbox: [Number(bbox[0]), Number(bbox[1]), Number(bbox[2]), Number(bbox[3])],
    });
  }
  return out;
}

const ZOOM_MIN = 1;
const ZOOM_MAX = 4;
const DOUBLE_TAP_MS = 280;
const clamp = (value: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, value));

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The displayed rect of the page at a given zoom + pan: the contain-fit rect
 *  scaled about its centre, then translated by the pan offset. */
function displayedRect(cw: number, ch: number, ratio: number, zoom: number, panX: number, panY: number): Rect {
  const base = containRect(cw, ch, ratio);
  const w = base.w * zoom;
  const h = base.h * zoom;
  return { x: base.x - (w - base.w) / 2 + panX, y: base.y - (h - base.h) / 2 + panY, w, h };
}

function touchDistance(touches: NativeTouchEvent[]): number {
  const a = touches[0];
  const b = touches[1];
  if (!a || !b) return 0;
  return Math.hypot(a.pageX - b.pageX, a.pageY - b.pageY);
}

/**
 * The tablet read-along (kinora.md §5.2): the **real rasterised page**, with the
 * spoken word lit in place over the image (karaoke on the raster, §5.3),
 * tap-to-seek, and **pinch-to-zoom / double-tap / drag-to-pan** so a reader can
 * magnify fine print or a manga panel. The page turns with the playhead (the
 * parent feeds `page`), and zoom/pan reset on each page.
 *
 * The phone stays on the reflow column; this pane only appears where there's room.
 */
export function PdfPageView({
  bookId,
  page,
  highlightWordIndex,
  onSeekWord,
}: {
  bookId: string;
  page: number;
  highlightWordIndex: number | null;
  onSeekWord: (word: number) => void;
}) {
  const [container, setContainer] = useState({ w: 0, h: 0 });
  const [ratio, setRatio] = useState<number | null>(null); // intrinsic width / height
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });

  // A new page may have a different aspect; clear zoom/pan/aspect so the overlay
  // never uses the previous page's geometry.
  useEffect(() => {
    setRatio(null);
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [page]);

  const { data, isLoading } = useQuery({
    queryKey: queryKeys.page(bookId, page),
    enabled: page > 0,
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: bookId, page_number: page } },
      });
      if (error || !data) throw new Error("failed to load page");
      return data;
    },
  });

  const words = parseWordBoxes(data?.word_boxes as ReadonlyArray<Record<string, unknown>> | undefined);
  const imageUrl = data?.image_url ?? null;
  const displayed = ratio && container.w > 0 ? displayedRect(container.w, container.h, ratio, zoom, pan.x, pan.y) : null;
  const active = highlightWordIndex !== null ? words.find((w) => w.word_index === highlightWordIndex) : undefined;

  // Latest values for the (once-created) gesture recogniser to read.
  const live = useRef({ container, ratio, zoom, pan, words, onSeekWord });
  live.current = { container, ratio, zoom, pan, words, onSeekWord };
  const gesture = useRef({ mode: "none" as "none" | "pinch" | "pan", startDist: 0, startZoom: 1, startX: 0, startY: 0, moved: false });
  const lastTap = useRef(0);
  const tapTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (tapTimer.current) clearTimeout(tapTimer.current);
  }, []);

  const clampPan = (px: number, py: number, zoomNow: number): { x: number; y: number } => {
    const { container: c, ratio: r } = live.current;
    if (!r) return { x: 0, y: 0 };
    const base = containRect(c.w, c.h, r);
    const overflowX = Math.max(0, base.w * zoomNow - c.w);
    const overflowY = Math.max(0, base.h * zoomNow - c.h);
    const maxX = overflowX / 2 + base.x + 8;
    const maxY = overflowY / 2 + base.y + 8;
    return { x: clamp(px, -maxX, maxX), y: clamp(py, -maxY, maxY) };
  };

  const seekAt = (tx: number, ty: number) => {
    const { container: c, ratio: r, zoom: z, pan: p, words: ws, onSeekWord: seek } = live.current;
    if (!r || c.w === 0) return;
    const d = displayedRect(c.w, c.h, r, z, p.x, p.y);
    const nx = (tx - d.x) / d.w;
    const ny = (ty - d.y) / d.h;
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return;
    const hit = wordAtNormalisedPoint(ws, nx, ny);
    if (hit !== null) seek(hit);
  };

  const responder = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => true,
      onMoveShouldSetPanResponder: (e, g) =>
        e.nativeEvent.touches.length === 2 || live.current.zoom > 1.01 || Math.abs(g.dx) + Math.abs(g.dy) > 6,
      onPanResponderGrant: (e) => {
        gesture.current.moved = false;
        const touches = e.nativeEvent.touches;
        if (touches.length === 2) {
          gesture.current.mode = "pinch";
          gesture.current.startDist = touchDistance(touches);
          gesture.current.startZoom = live.current.zoom;
        } else {
          gesture.current.mode = live.current.zoom > 1.01 ? "pan" : "none";
          gesture.current.startX = live.current.pan.x;
          gesture.current.startY = live.current.pan.y;
        }
      },
      onPanResponderMove: (e, g) => {
        const touches = e.nativeEvent.touches;
        if (touches.length === 2) {
          gesture.current.mode = "pinch";
          const dist = touchDistance(touches);
          if (!gesture.current.startDist) {
            gesture.current.startDist = dist;
            gesture.current.startZoom = live.current.zoom;
          }
          const z = clamp(gesture.current.startZoom * (dist / gesture.current.startDist), ZOOM_MIN, ZOOM_MAX);
          gesture.current.moved = true;
          setZoom(z);
          if (z <= 1.01) setPan({ x: 0, y: 0 });
          else setPan((prev) => clampPan(prev.x, prev.y, z));
        } else if (gesture.current.mode === "pan") {
          if (Math.abs(g.dx) + Math.abs(g.dy) > 4) gesture.current.moved = true;
          setPan(clampPan(gesture.current.startX + g.dx, gesture.current.startY + g.dy, live.current.zoom));
        }
      },
      onPanResponderRelease: (e, g) => {
        const moved = gesture.current.moved || Math.abs(g.dx) + Math.abs(g.dy) > 6;
        gesture.current.mode = "none";
        gesture.current.startDist = 0;
        if (moved) return;
        const { locationX, locationY } = e.nativeEvent;
        const now = Date.now();
        if (now - lastTap.current < DOUBLE_TAP_MS) {
          lastTap.current = 0;
          if (tapTimer.current) {
            clearTimeout(tapTimer.current);
            tapTimer.current = null;
          }
          // Double-tap toggles between fit and 2× (resetting pan on the way back).
          if (live.current.zoom > 1.01) {
            setZoom(1);
            setPan({ x: 0, y: 0 });
          } else {
            setZoom(2);
          }
        } else {
          lastTap.current = now;
          // Defer the seek so a second tap can cancel it (double-tap = zoom).
          tapTimer.current = setTimeout(() => {
            tapTimer.current = null;
            seekAt(locationX, locationY);
          }, DOUBLE_TAP_MS);
        }
      },
      onPanResponderTerminationRequest: () => false,
    }),
  ).current;

  const onLayout = (event: LayoutChangeEvent) => {
    const { width, height } = event.nativeEvent.layout;
    setContainer({ w: width, h: height });
  };

  return (
    <View style={styles.container} onLayout={onLayout} {...responder.panHandlers}>
      {imageUrl ? (
        <>
          <Image
            source={{ uri: imageUrl }}
            resizeMode="contain"
            onLoad={(event) => {
              const src = event.nativeEvent.source;
              if (src && src.width > 0 && src.height > 0) setRatio(src.width / src.height);
            }}
            style={
              displayed
                ? { position: "absolute", left: displayed.x, top: displayed.y, width: displayed.w, height: displayed.h }
                : StyleSheet.absoluteFill
            }
          />
          {displayed && active ? (
            <View
              pointerEvents="none"
              style={[
                styles.highlight,
                {
                  left: displayed.x + active.bbox[0] * displayed.w,
                  top: displayed.y + active.bbox[1] * displayed.h,
                  width: active.bbox[2] * displayed.w,
                  height: active.bbox[3] * displayed.h,
                },
              ]}
            />
          ) : null}
        </>
      ) : isLoading || data === undefined ? (
        <View style={styles.center}>
          <ActivityIndicator color={palette.emberGlow} />
        </View>
      ) : (
        // No rasterised image — fall back to the page's text (§12.4 ladder).
        <ScrollView contentContainerStyle={styles.fallbackContent}>
          <Text style={styles.fallbackText}>{data?.text ?? "This page has no text yet."}</Text>
        </ScrollView>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: palette.walnutDeep, overflow: "hidden" },
  center: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
  },
  highlight: {
    position: "absolute",
    backgroundColor: "rgba(244,168,93,0.34)",
    borderRadius: 3,
  },
  fallbackContent: { padding: space.xl },
  fallbackText: {
    color: palette.parchment,
    fontSize: type.reading.fontSize,
    lineHeight: type.reading.lineHeight,
  },
});
