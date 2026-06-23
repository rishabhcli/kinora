import { useRef, useState } from "react";
import {
  type LayoutChangeEvent,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import {
  AmbientBackdrop,
  GhostButton,
  KinoraMark,
  PrimaryButton,
  Surface,
  Wordmark,
} from "../components/ui";
import { useReducedMotion } from "../hooks/useReducedMotion";
import {
  alpha,
  BOTTOM_INSET,
  fonts,
  HIT_TARGET,
  palette,
  radius,
  space,
  TOP_INSET,
  type,
} from "../theme/tokens";

type Slide = {
  /** A short uppercase eyebrow over the headline. */
  eyebrow: string;
  /** The serif headline — the promise. */
  title: string;
  /** One or two crisp sentences of supporting copy. */
  body: string;
  /** A bespoke, dependency-free "scene" drawn for this slide. */
  scene: () => React.ReactElement;
};

const SLIDES: Slide[] = [
  {
    eyebrow: "Welcome to Kinora",
    title: "Watch the book.",
    body: "Open any book and a film unfolds alongside the words — your story, on screen, page for page.",
    scene: () => <HeroScene />,
  },
  {
    eyebrow: "Always a step ahead",
    title: "A film that runs ahead of you.",
    body: "Kinora reads with you and renders the next scenes a few seconds early, so the picture is always waiting as you turn the page.",
    scene: () => <SyncScene />,
  },
  {
    eyebrow: "Your shelf, your stories",
    title: "Bring in any book.",
    body: "Drop in a PDF or EPUB and Kinora begins the adaptation — from the classics to the manuscript you just finished.",
    scene: () => <ImportScene />,
  },
  {
    eyebrow: "Faithful, end to end",
    title: "Consistent to the last page.",
    body: "Six AI agents share one evolving canon, keeping faces, places and tone steady across a whole novel.",
    scene: () => <CanonScene />,
  },
];

/**
 * The first-run intro: a swipeable carousel introducing Kinora over the warm
 * screening-room backdrop. Built from the existing design system only — the
 * ambient backdrop, glass surfaces, the serif wordmark, the ember button and the
 * type/spacing tokens.
 *
 * A horizontally paged `ScrollView` carries one full-width slide each; page dots
 * track position and Skip / Next (Get started on the last slide) advance it.
 * Auto-advance is intentionally omitted entirely (we never move the page for the
 * reader) so the flow is calm and respects reduce-motion by construction.
 */
export function OnboardingScreen({ onDone }: { onDone: () => void }) {
  const reduced = useReducedMotion();
  const [width, setWidth] = useState(0);
  const [index, setIndex] = useState(0);
  const scrollRef = useRef<ScrollView>(null);

  const last = index >= SLIDES.length - 1;

  function onLayout(e: LayoutChangeEvent) {
    setWidth(e.nativeEvent.layout.width);
  }

  function onScroll(e: NativeSyntheticEvent<NativeScrollEvent>) {
    if (width <= 0) return;
    const next = Math.round(e.nativeEvent.contentOffset.x / width);
    if (next !== index) setIndex(Math.max(0, Math.min(SLIDES.length - 1, next)));
  }

  function goTo(next: number) {
    const clamped = Math.max(0, Math.min(SLIDES.length - 1, next));
    setIndex(clamped);
    scrollRef.current?.scrollTo({ x: clamped * width, animated: !reduced });
  }

  function next() {
    if (last) onDone();
    else goTo(index + 1);
  }

  return (
    <AmbientBackdrop>
      <View style={styles.root} onLayout={onLayout}>
        <View style={styles.topBar}>
          <Wordmark size={type.title.fontSize} withMark align="left" />
          {!last ? <GhostButton label="Skip" onPress={onDone} align="right" /> : <View style={styles.skipSpacer} />}
        </View>

        {width > 0 ? (
          <ScrollView
            ref={scrollRef}
            horizontal
            pagingEnabled
            showsHorizontalScrollIndicator={false}
            onScroll={onScroll}
            scrollEventThrottle={16}
            style={styles.pager}
            contentContainerStyle={styles.pagerContent}
            accessibilityLabel="Introduction to Kinora"
          >
            {SLIDES.map((slide, i) => (
              <SlideView key={slide.title} slide={slide} width={width} active={i === index} />
            ))}
          </ScrollView>
        ) : (
          <View style={styles.pager} />
        )}

        <View style={styles.footer}>
          <Dots count={SLIDES.length} index={index} onPress={goTo} />
          <View style={styles.cta}>
            <PrimaryButton label={last ? "Get started" : "Next"} onPress={next} />
          </View>
        </View>
      </View>
    </AmbientBackdrop>
  );
}

function SlideView({ slide, width, active }: { slide: Slide; width: number; active: boolean }) {
  return (
    <View style={[styles.slide, { width }]}>
      <View style={styles.sceneWrap}>{slide.scene()}</View>
      <View style={styles.copy}>
        <Text style={styles.eyebrow}>{slide.eyebrow}</Text>
        <Text
          style={styles.title}
          accessibilityRole="header"
          // Only the on-screen slide should be reachable as a heading by the screen reader.
          accessibilityElementsHidden={!active}
          importantForAccessibility={active ? "yes" : "no-hide-descendants"}
        >
          {slide.title}
        </Text>
        <Text style={styles.body}>{slide.body}</Text>
      </View>
    </View>
  );
}

/** The page indicator — a row of dots; the active one widens into an ember pill. */
function Dots({ count, index, onPress }: { count: number; index: number; onPress: (i: number) => void }) {
  return (
    <View style={styles.dots} accessibilityRole="tablist">
      {Array.from({ length: count }, (_, i) => {
        const active = i === index;
        return (
          <Pressable
            key={i}
            onPress={() => onPress(i)}
            hitSlop={10}
            accessibilityRole="tab"
            accessibilityState={{ selected: active }}
            accessibilityLabel={`Slide ${i + 1} of ${count}`}
          >
            <View style={[styles.dot, active && styles.dotActive]} />
          </Pressable>
        );
      })}
    </View>
  );
}

/* ── Scenes ───────────────────────────────────────────────────────────────
   Each slide's illustration, drawn from plain Views over a glass Surface so we
   stay dependency-free (the same approach as KinoraMark / SearchField). They're
   warm, abstract "frames" rather than literal art. */

/** Slide 1: the mark, enlarged, glowing inside a film frame on glass. */
function HeroScene() {
  return (
    <SceneCard>
      <View style={scene.haloOuter} />
      <View style={scene.haloInner} />
      <KinoraMark size={84} />
    </SceneCard>
  );
}

/** Slide 2: a page of text on the left and a lit film frame on the right, the
 *  reader's line and the rendered frame aligned — the page-synced playhead. */
function SyncScene() {
  return (
    <SceneCard>
      <View style={scene.syncRow}>
        <View style={scene.pageMini}>
          {[0.9, 1, 0.95, 0.6, 0.85, 1, 0.7].map((w, i) => (
            <View key={i} style={[scene.textLine, { width: `${w * 100}%` }, i === 3 && scene.textLineLit]} />
          ))}
        </View>
        <View style={scene.linkDots}>
          <View style={scene.linkDot} />
          <View style={scene.linkDot} />
          <View style={scene.linkDot} />
        </View>
        <View style={scene.frameMini}>
          <View style={scene.frameGlow} />
          <View style={scene.playTri} />
          <View style={scene.scrub}>
            <View style={scene.scrubFill} />
          </View>
        </View>
      </View>
    </SceneCard>
  );
}

/** Slide 3: a document with a folded corner dropping toward a frame — import. */
function ImportScene() {
  return (
    <SceneCard>
      <View style={scene.doc}>
        <View style={scene.docFold} />
        {[0.7, 0.9, 0.5, 0.8].map((w, i) => (
          <View key={i} style={[scene.docLine, { width: `${w * 100}%` }]} />
        ))}
        <View style={scene.docBadge}>
          <Text style={scene.docBadgeText}>PDF · EPUB</Text>
        </View>
      </View>
      <View style={scene.importArrow} />
    </SceneCard>
  );
}

/** Slide 4: six small frames orbiting one bright canon core — shared memory. */
function CanonScene() {
  const ring = 64;
  const nodes = Array.from({ length: 6 }, (_, i) => {
    const angle = (Math.PI * 2 * i) / 6 - Math.PI / 2;
    return { x: Math.cos(angle) * ring, y: Math.sin(angle) * ring };
  });
  return (
    <SceneCard>
      <View style={scene.canonField}>
        {nodes.map((_n, i) => (
          <View key={i} style={[scene.canonLink, { width: ring, transform: [{ rotate: `${(360 / 6) * i + 90}deg` }] }]} />
        ))}
        <View style={scene.canonCore}>
          <KinoraMark size={30} />
        </View>
        {nodes.map((n, i) => (
          <View key={i} style={[scene.canonNode, { transform: [{ translateX: n.x }, { translateY: n.y }] }]} />
        ))}
      </View>
    </SceneCard>
  );
}

/** The shared glass stage every scene sits on. */
function SceneCard({ children }: { children: React.ReactNode }) {
  return (
    <Surface style={scene.card}>
      <View style={scene.cardInner}>{children}</View>
    </Surface>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  topBar: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: space.xxl,
    paddingTop: TOP_INSET,
    paddingBottom: space.md,
    minHeight: TOP_INSET + HIT_TARGET,
  },
  skipSpacer: { width: 44, height: HIT_TARGET - 12 },
  pager: { flex: 1 },
  pagerContent: { alignItems: "stretch" },
  slide: {
    flex: 1,
    paddingHorizontal: space.xxl,
    justifyContent: "center",
  },
  sceneWrap: { alignItems: "center", justifyContent: "center" },
  copy: { marginTop: space.huge, maxWidth: 480, alignSelf: "center", width: "100%" },
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
    marginTop: space.sm,
  },
  body: {
    color: alpha.white72,
    fontSize: type.body.fontSize,
    lineHeight: type.reading.lineHeight,
    marginTop: space.md,
  },
  footer: {
    paddingHorizontal: space.xxl,
    paddingTop: space.lg,
    paddingBottom: BOTTOM_INSET + space.lg,
    gap: space.xl,
  },
  dots: { flexDirection: "row", justifyContent: "center", alignItems: "center", gap: space.sm },
  dot: {
    width: 7,
    height: 7,
    borderRadius: radius.pill,
    backgroundColor: alpha.white16,
  },
  dotActive: { width: 22, backgroundColor: palette.emberGlow },
  cta: { width: "100%", maxWidth: 480, alignSelf: "center" },
});

const scene = StyleSheet.create({
  card: { width: "100%", maxWidth: 480, alignSelf: "center", aspectRatio: 1.5, maxHeight: 320 },
  cardInner: { flex: 1, alignItems: "center", justifyContent: "center" },

  // Slide 1 — hero halo.
  haloOuter: {
    position: "absolute",
    width: 220,
    height: 220,
    borderRadius: 110,
    backgroundColor: "rgba(224,134,58,0.10)",
  },
  haloInner: {
    position: "absolute",
    width: 130,
    height: 130,
    borderRadius: 65,
    backgroundColor: "rgba(244,168,93,0.14)",
  },

  // Slide 2 — sync row.
  syncRow: { flexDirection: "row", alignItems: "center", paddingHorizontal: space.xl },
  pageMini: { flex: 1, gap: 7, paddingRight: space.sm },
  textLine: { height: 5, borderRadius: 3, backgroundColor: alpha.white16 },
  textLineLit: { backgroundColor: palette.emberGlow, opacity: 0.95 },
  linkDots: { gap: 4, marginHorizontal: space.md },
  linkDot: { width: 4, height: 4, borderRadius: 2, backgroundColor: alpha.white40 },
  frameMini: {
    width: 118,
    height: 78,
    borderRadius: radius.sm,
    borderWidth: 1.5,
    borderColor: alpha.white16,
    backgroundColor: "rgba(0,0,0,0.30)",
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
  },
  frameGlow: {
    position: "absolute",
    top: -28,
    width: 120,
    height: 80,
    borderRadius: 60,
    backgroundColor: "rgba(244,168,93,0.16)",
  },
  playTri: {
    width: 0,
    height: 0,
    borderTopWidth: 9,
    borderBottomWidth: 9,
    borderLeftWidth: 15,
    borderTopColor: "transparent",
    borderBottomColor: "transparent",
    borderLeftColor: palette.emberGlow,
    marginLeft: 4,
  },
  scrub: {
    position: "absolute",
    bottom: 8,
    left: 10,
    right: 10,
    height: 3,
    borderRadius: 2,
    backgroundColor: alpha.white12,
    overflow: "hidden",
  },
  scrubFill: { width: "62%", height: "100%", borderRadius: 2, backgroundColor: palette.ember },

  // Slide 3 — import.
  doc: {
    width: 150,
    height: 190,
    borderRadius: radius.sm,
    backgroundColor: alpha.white08,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.white16,
    padding: space.lg,
    gap: 9,
    ...Platform.select({
      ios: { shadowColor: "#000", shadowOffset: { width: 0, height: 10 }, shadowOpacity: 0.4, shadowRadius: 18 },
      android: { elevation: 8 },
      default: {},
    }),
  },
  docFold: {
    position: "absolute",
    top: 0,
    right: 0,
    width: 26,
    height: 26,
    borderTopRightRadius: radius.sm,
    backgroundColor: alpha.white12,
  },
  docLine: { height: 6, borderRadius: 3, backgroundColor: alpha.white16 },
  docBadge: {
    marginTop: "auto",
    alignSelf: "flex-start",
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: radius.pill,
    backgroundColor: alpha.emberSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: alpha.glassFieldFocus,
  },
  docBadgeText: {
    color: palette.emberGlow,
    fontSize: type.micro.fontSize,
    fontWeight: "700",
    letterSpacing: 1,
  },
  importArrow: {
    position: "absolute",
    bottom: 34,
    right: 56,
    width: 0,
    height: 0,
    borderLeftWidth: 11,
    borderRightWidth: 11,
    borderTopWidth: 16,
    borderLeftColor: "transparent",
    borderRightColor: "transparent",
    borderTopColor: palette.ember,
    opacity: 0.85,
  },

  // Slide 4 — canon orbit.
  canonField: { width: 168, height: 168, alignItems: "center", justifyContent: "center" },
  canonCore: {
    width: 60,
    height: 60,
    borderRadius: 30,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: alpha.emberSoft,
    borderWidth: 1.5,
    borderColor: alpha.glassFieldFocus,
  },
  canonLink: {
    position: "absolute",
    height: StyleSheet.hairlineWidth,
    backgroundColor: alpha.white16,
    transformOrigin: "left center",
  },
  canonNode: {
    position: "absolute",
    width: 14,
    height: 14,
    borderRadius: 4,
    backgroundColor: "rgba(0,0,0,0.35)",
    borderWidth: 1.2,
    borderColor: palette.emberGlow,
  },
});
