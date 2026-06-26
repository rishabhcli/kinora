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
   *  scrubber); otherwise the clip plays forward (ambient scene motion). `src===""`
   *  holds the last frame (live clip still generating — never blank the pane). */
  setPlayhead(src: string, time: number, scrub: boolean): void;
  /** The active `<video>`'s duration (s), once known — for the unknown-length
   *  bundled fallback film. 0 until metadata loads. */
  getActiveDuration(): number;
}

interface FilmPaneProps {
  poster?: string;
  reducedMotion: boolean;
  /** live mode, no clip yet → show the generating spinner instead of nothing */
  generating?: boolean;
  className?: string;
  style?: CSSProperties;
}

interface Layer {
  key: number;
  src: string;
}

const SEEK_EPSILON = 1 / 30; // don't re-seek within ~1 frame — avoids seek thrash
const FADE_S = 0.5; // event/scene crossfade when settling (not while scrubbing)

/** The vertical AI film. Holds ≤2 `<video>` layers and crossfades (opacity only)
 *  when the `src` changes *and* we're settling — i.e. at event/scene boundaries.
 *  While scrubbing (or under reduced motion) it hard-cuts, like a real scrubber.
 *  Segments that share a `src` (a stitched event film) never trigger a layer
 *  change — they're scrubbed by `currentTime` alone. */
export const FilmPane = forwardRef<FilmPaneHandle, FilmPaneProps>(function FilmPane(
  { poster, reducedMotion, generating, className, style },
  ref,
) {
  const [layers, setLayers] = useState<Layer[]>([]);
  // `revealKey` is the layer currently at opacity 1 (the other fades under it).
  const [revealKey, setRevealKey] = useState(-1);

  const keySeq = useRef(0);
  const els = useRef(new Map<number, HTMLVideoElement>());
  const layersRef = useRef<Layer[]>([]); // synchronous mirror of `layers`
  layersRef.current = layers;
  const targetSrc = useRef("");
  const pendingTime = useRef(0);

  const activeEl = useCallback((): HTMLVideoElement | undefined => {
    const ls = layersRef.current;
    return ls.length ? els.current.get(ls[ls.length - 1].key) : undefined;
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
      if (Number.isFinite(time)) seek(el, time);
    } else if (el.paused) {
      void el.play().catch(() => {}); // muted autoplay; ignore policy rejections
    }
  };

  useImperativeHandle(
    ref,
    (): FilmPaneHandle => ({
      setPlayhead(src, time, scrub) {
        if (!src) return; // generating the next clip — keep the last frame on screen
        if (src === targetSrc.current) {
          // A flick that interrupts a settle-crossfade hard-cuts to the active layer
          // (scrubbing shows frames, not fades).
          if (scrub && layersRef.current.length === 2) {
            const top = layersRef.current[1];
            setLayers([top]);
            setRevealKey(top.key);
          }
          applyActive(time, scrub); // same film → imperative seek/play, no re-render
          return;
        }
        // The film changed (event/scene boundary).
        targetSrc.current = src;
        pendingTime.current = time;
        const key = ++keySeq.current;
        const top = layersRef.current[layersRef.current.length - 1];
        if (reducedMotion || scrub || !top) {
          // Hard cut: a single layer. (Scrubbing through scenes shows frames, not
          // crossfades; reduced motion never crossfades; nor does the first film.)
          setLayers([{ key, src }]);
          setRevealKey(key);
        } else {
          // Settling onto a new scene → crossfade: keep the current layer (it stays
          // revealed), fade the incoming one in once it has decoded the target frame.
          setLayers([top, { key, src }]);
        }
      },
      getActiveDuration() {
        const d = activeEl()?.duration;
        return d && Number.isFinite(d) ? d : 0;
      },
    }),
    // applyActive/activeEl are stable; reducedMotion is read live.
    [reducedMotion], // eslint-disable-line react-hooks/exhaustive-deps
  );

  // A newly-mounted incoming layer decoded its target frame → reveal it (start the
  // crossfade). We deliberately do NOT touch currentTime here: `onSeeked` fires on
  // EVERY seek, including the rAF loop's live scrub seeks, so re-seeking here would
  // yank the playhead back to the entry frame and defeat scrubbing. `currentTime` is
  // owned solely by `applyActive`; initial play by the `autoPlay` attribute.
  const onReady = (key: number) => {
    const ls = layersRef.current;
    if (ls.length === 2 && ls[1].key === key) setRevealKey(key); // begin fade-in
  };

  // Seek the incoming layer to the target frame as soon as it can.
  const onMeta = (key: number) => {
    const el = els.current.get(key);
    if (el && Number.isFinite(pendingTime.current)) seek(el, pendingTime.current);
  };

  // Crossfade finished → drop the outgoing layer (cap at the single visible one).
  const onFadeEnd = (key: number) => {
    setLayers((prev) =>
      prev.length === 2 && prev[1].key === key && key === revealKey ? [prev[1]] : prev,
    );
  };

  if (layers.length === 0) {
    return generating ? (
      <div className={className} style={style}>
        <div className="absolute inset-0 grid place-items-center bg-black/60">
          <div className="flex flex-col items-center gap-3 text-center">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-white/15 border-t-white/60" />
            <p className="text-[11px] text-white/60">Generating your film…</p>
          </div>
        </div>
      </div>
    ) : (
      <div className={className} style={style} />
    );
  }

  // The visible layer. Normally `revealKey`, but if a rapid second src change has
  // dropped that layer, fall back to the oldest still-mounted one (most likely
  // already decoded) so a layer is *always* visible — never a blank/black pane.
  const shownKey = layers.some((l) => l.key === revealKey) ? revealKey : layers[0]?.key;

  return (
    <div className={className} style={style}>
      {layers.map((l) => (
        <video
          key={l.key}
          ref={(el) => {
            if (el) els.current.set(l.key, el);
            else els.current.delete(l.key);
          }}
          src={l.src}
          poster={poster}
          autoPlay
          muted
          loop
          playsInline
          preload="auto"
          onLoadedMetadata={() => onMeta(l.key)}
          onCanPlay={() => onReady(l.key)}
          onSeeked={() => onReady(l.key)}
          onTransitionEnd={(e) => {
            if (e.propertyName === "opacity") onFadeEnd(l.key);
          }}
          className="absolute inset-0 h-full w-full bg-black object-cover"
          style={{
            opacity: layers.length === 1 || l.key === shownKey ? 1 : 0,
            transition: reducedMotion ? "none" : `opacity ${FADE_S}s ease`,
            willChange: "opacity",
          }}
        />
      ))}
    </div>
  );
});
