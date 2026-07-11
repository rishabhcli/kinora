import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useRef,
  useState,
  type CSSProperties,
} from "react";

/** The imperative surface the engine's rAF loop drives. Keeping the playhead
 *  imperative (not React state) is what gets us 60fps: `setPlayhead` is called
 *  every frame but only re-renders when the *film* (src) changes. */
export interface FilmPaneHandle {
  /** Show `src` at `time` seconds. `scrub` pins `currentTime` (the video is a
   *  scrubber); otherwise the clip plays forward. `src===""` deliberately blanks
   *  the pane while the real scene is still rendering. */
  setPlayhead(src: string, time: number, scrub: boolean): void;
  /** The active `<video>`'s duration (s), once known — for the unknown-length
   *  bundled fallback film. 0 until metadata loads. */
  getActiveDuration(): number;
  /** Warm an off-screen `<video>` with `src` so a (forward or backward) cut to it
   *  is instant — no re-create, no re-decode flicker. No-op for the active src or
   *  one already warm. The engine calls this for neighbour shots. */
  warm(src: string): void;
}

interface FilmPaneProps {
  reducedMotion: boolean;
  /** live mode, no clip yet → show the generating spinner instead of nothing */
  generating?: boolean;
  className?: string;
  style?: CSSProperties;
  /** Max distinct decoded <video> elements kept warm for instant scroll-back.
   *  The 2 active crossfade layers always count toward this. */
  poolSize?: number;
}

interface Slot {
  /** stable identity for this src — a returning src reuses its decoded element */
  key: number;
  src: string;
}

const SEEK_EPSILON = 1 / 30; // don't re-seek within ~1 frame — avoids seek thrash
const SCRUB_SEEK_INTERVAL_MS = 34; // cap live scrub seeks to roughly 30Hz
const FADE_S = 0.5; // event/scene crossfade when settling (not while scrubbing)
const DEFAULT_POOL = 4; // active 2 + ~2 neighbours kept warm for scroll-back

/** The vertical AI film. Holds ≤2 *visible* `<video>` layers and crossfades
 *  (opacity only) when the `src` changes *and* we're settling — i.e. at
 *  event/scene boundaries. While scrubbing (or under reduced motion) it
 *  hard-cuts, like a real scrubber. Segments that share a `src` (a stitched
 *  event film) never trigger a layer change — they're scrubbed by `currentTime`
 *  alone.
 *
 *  Beyond the visible layers it keeps a small POOL of recently-shown `<video>`
 *  elements mounted but hidden, each keyed by its `src`, so scrolling BACK to an
 *  earlier shot replays the SAME decoded element instantly — no element
 *  re-create, no re-fetch, no flash of empty video. The pool is an LRU capped at
 *  `poolSize`; the oldest non-visible element is dropped when it overflows. */
export const FilmPane = forwardRef<FilmPaneHandle, FilmPaneProps>(function FilmPane(
  { reducedMotion, generating, className, style, poolSize = DEFAULT_POOL },
  ref,
) {
  // The mounted set: 2 active crossfade layers + warm (hidden) neighbours, all
  // keyed by src so React reuses each decoded <video> across scroll-back.
  const [slots, setSlots] = useState<Slot[]>([]);
  // Identity of the layer currently revealed (opacity 1); -1 = none yet.
  const [revealKey, setRevealKey] = useState(-1);
  // Identity of the *previous* visible layer that's fading under the incoming one
  // during a crossfade. Both it and the incoming layer render visibly until the
  // fade ends; everything else in the pool is hidden (opacity 0, kept warm).
  const [fadingKey, setFadingKey] = useState(-1);

  const keySeq = useRef(0);
  const keyForSrc = useRef(new Map<string, number>()); // stable src → element key
  const els = useRef(new Map<number, HTMLVideoElement>());
  const slotsRef = useRef<Slot[]>([]); // synchronous mirror of `slots`
  slotsRef.current = slots;
  const targetSrc = useRef("");
  const pendingTime = useRef(0);
  const lastSeekAt = useRef(0);

  // Stable identity for a src — the same src always maps to the same <video>.
  const keyOf = useCallback((src: string): number => {
    let k = keyForSrc.current.get(src);
    if (k == null) {
      k = ++keySeq.current;
      keyForSrc.current.set(src, k);
    }
    return k;
  }, []);

  const activeEl = useCallback((): HTMLVideoElement | undefined => {
    const k = keyForSrc.current.get(targetSrc.current);
    return k != null ? els.current.get(k) : undefined;
  }, []);

  const seek = (el: HTMLVideoElement, time: number) => {
    if (Math.abs(el.currentTime - time) <= SEEK_EPSILON) return;
    // fastSeek (Safari/Firefox) is cheaper for live scrubbing; Chromium lacks it.
    if (typeof el.fastSeek === "function") el.fastSeek(time);
    else el.currentTime = time;
  };

  const applyActive = (time: number, scrub: boolean) => {
    const el = activeEl();
    if (!el) return;
    if (scrub) {
      if (!el.paused) el.pause();
      const now = performance.now();
      if (Number.isFinite(time) && now - lastSeekAt.current >= SCRUB_SEEK_INTERVAL_MS) {
        lastSeekAt.current = now;
        seek(el, time);
      }
    } else if (el.paused) {
      void el.play().catch(() => {}); // muted autoplay; ignore policy rejections
    }
  };

  // Insert/raise `key` in the LRU pool, evicting the oldest non-visible slot when
  // we exceed `poolSize`. The 2 visible layers (`keep`) are never evicted.
  const ensureSlot = useCallback(
    (prev: Slot[], key: number, src: string, keep: Set<number>): Slot[] => {
      const without = prev.filter((s) => s.key !== key);
      const next = [...without, { key, src }]; // most-recently-used at the end
      const cap = Math.max(2, poolSize);
      if (next.length <= cap) return next;
      const survivors: Slot[] = [];
      let toDrop = next.length - cap;
      for (const s of next) {
        if (toDrop > 0 && s.key !== key && !keep.has(s.key)) {
          toDrop--;
          keyForSrc.current.delete(s.src);
          continue; // evicted — React unmounts its <video>
        }
        survivors.push(s);
      }
      return survivors;
    },
    [poolSize],
  );

  useImperativeHandle(
    ref,
    (): FilmPaneHandle => ({
      setPlayhead(src, time, scrub) {
        if (!src) {
          activeEl()?.pause();
          targetSrc.current = "";
          setRevealKey(-1);
          setFadingKey(-1);
          return;
        }
        if (src === targetSrc.current) {
          // A flick that interrupts a settle-crossfade hard-cuts to the active layer
          // (scrubbing shows frames, not fades).
          if (scrub && fadingKey !== -1) {
            const activeKey = keyForSrc.current.get(src);
            if (activeKey != null) setRevealKey(activeKey);
            setFadingKey(-1);
          }
          applyActive(time, scrub); // same film → imperative seek/play, no re-render
          return;
        }
        // The film changed (event/scene boundary).
        targetSrc.current = src;
        pendingTime.current = time;
        lastSeekAt.current = 0;
        const key = keyOf(src);
        const outgoing = revealKey;
        const reused = els.current.has(key); // already decoded → instant replay

        if (reducedMotion || scrub || outgoing === -1) {
          // Hard cut: reveal the target immediately. (Scrubbing through scenes
          // shows frames, not crossfades; reduced motion never crossfades; nor
          // does the first film.) A reused element is already decoded, so this is
          // a flash-free instant cut — the whole point of the warm pool.
          setRevealKey(key);
          setFadingKey(-1);
          setSlots((prev) => ensureSlot(prev, key, src, new Set([key])));
          if (reused) applyActive(time, scrub);
        } else {
          // Settling onto a new scene → crossfade: the current layer stays
          // revealed (fading out) while the incoming one fades in once decoded.
          setFadingKey(outgoing);
          setSlots((prev) => ensureSlot(prev, key, src, new Set([key, outgoing])));
          if (reused) {
            applyActive(time, scrub); // already decoded → seek/play to the target frame
            onReady(key); // …and start the fade now (no decode wait)
          }
        }
      },
      getActiveDuration() {
        const d = activeEl()?.duration;
        return d && Number.isFinite(d) ? d : 0;
      },
      warm(src) {
        if (!src || src === targetSrc.current) return;
        const key = keyOf(src);
        if (els.current.has(key)) return; // already warm
        // Mount it hidden (kept out of the crossfade) so its bytes decode ahead of
        // the reader; never evict the active/fading layers to make room.
        const keep = new Set<number>();
        if (revealKey !== -1) keep.add(revealKey);
        if (fadingKey !== -1) keep.add(fadingKey);
        setSlots((prev) => (prev.some((s) => s.key === key) ? prev : ensureSlot(prev, key, src, keep)));
      },
    }),
    // applyActive/activeEl/keyOf/ensureSlot are stable; reducedMotion + the reveal
    // keys are read live.
    [reducedMotion, revealKey, fadingKey], // eslint-disable-line react-hooks/exhaustive-deps
  );

  // A mounted layer decoded its target frame → if it's the incoming crossfade
  // layer, reveal it (start the fade). We deliberately do NOT touch currentTime
  // here: `onSeeked` fires on EVERY seek, including the rAF loop's live scrub
  // seeks, so re-seeking here would yank the playhead back to the entry frame and
  // defeat scrubbing. `currentTime` is owned solely by `applyActive`; initial play
  // by the `autoPlay` attribute.
  const onReady = (key: number) => {
    setRevealKey((cur) => {
      // Only promote the layer that matches the current target src (the incoming
      // crossfade layer). Warm pool elements firing canplay must not steal reveal.
      if (key === keyForSrc.current.get(targetSrc.current)) return key;
      return cur;
    });
  };

  // Seek a layer to the target frame as soon as it has metadata — but only the
  // active target layer (warm-pool neighbours seek when they become active).
  const onMeta = (key: number) => {
    if (key !== keyForSrc.current.get(targetSrc.current)) return;
    const el = els.current.get(key);
    if (el && Number.isFinite(pendingTime.current)) seek(el, pendingTime.current);
  };

  // Crossfade finished → the incoming layer is fully revealed; clear the fading
  // marker so the outgoing layer drops back to the hidden warm pool (it stays
  // mounted for instant scroll-back, just no longer visible).
  const onFadeEnd = (key: number) => {
    setFadingKey((f) => (key === revealKey ? -1 : f));
  };

  if (slots.length === 0) {
    return (
      <div className={className} style={{ ...style, background: "#080808" }}>
        {generating && (
          <div className="absolute inset-0 grid place-items-center">
            <div className="h-px w-10 animate-pulse bg-white/30" aria-label="Rendering scene" />
          </div>
        )}
      </div>
    );
  }

  // A layer is visible iff it's the revealed one or the one currently fading out
  // under it. Everything else is a warm pool member, mounted at opacity 0 so it
  // stays decoded for instant scroll-back without painting over the film.
  // If rapid source churn leaves the revealed key unmounted, use the newest slot
  // only when there is still a real target source. Empty targets remain blank.
  const revealedMounted = slots.some((s) => s.key === revealKey);
  const shownKey = targetSrc.current
    ? revealedMounted
      ? revealKey
      : slots[slots.length - 1]?.key
    : undefined;

  return (
    <div className={className} style={style}>
      {slots.map((s) => {
        const visible = s.key === shownKey || s.key === fadingKey;
        return (
          <video
            key={s.key}
            ref={(el) => {
              if (el) els.current.set(s.key, el);
              else els.current.delete(s.key);
            }}
            src={s.src}
            autoPlay
            muted
            loop
            playsInline
            preload="auto"
            onLoadedMetadata={() => onMeta(s.key)}
            onCanPlay={() => onReady(s.key)}
            onSeeked={() => onReady(s.key)}
            onTransitionEnd={(e) => {
              if (e.propertyName === "opacity") onFadeEnd(s.key);
            }}
            className="absolute inset-0 h-full w-full bg-black object-cover"
            style={{
              opacity: visible && s.key === shownKey ? 1 : 0,
              // Hidden warm-pool members are non-interactive and out of the paint
              // path as much as possible; only the visible/fading layers transition.
              transition: reducedMotion || !visible ? "none" : `opacity ${FADE_S}s ease`,
              willChange: visible ? "opacity" : undefined,
              pointerEvents: "none",
            }}
          />
        );
      })}
      {shownKey == null && (
        <div className="absolute inset-0 grid place-items-center bg-[#080808]">
          <div className="h-px w-10 animate-pulse bg-white/30" aria-label="Rendering scene" />
        </div>
      )}
    </div>
  );
});
