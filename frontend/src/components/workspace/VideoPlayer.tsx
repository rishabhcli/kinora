import { type MutableRefObject, type RefObject, useEffect, useRef, useState } from "react";

import { kenBurnsStyle } from "../../lib/kenburns";
import { PauseIcon, PlayIcon } from "../common/icons";

interface VideoPlayerProps {
  videoRef: RefObject<HTMLVideoElement>;
  src: string | null;
  preloadSrc: string | null;
  bridging: boolean;
  bridgeKeyframeUrl: string | null;
  seekNonce: number;
  seekToS: number;
  playing: boolean;
  onTogglePlay: () => void;
  onTime: (t: number) => void;
  onEnded: () => void;
  bridgeSeed?: string | null;
}

// The stage runs TWO stacked <video> elements (kinora.md §5.2 / §5.6): one is
// visible, the other is a hidden buffer that warms the next clip (`preloadSrc`).
// When the engine swaps `src` to the clip the buffer has already decoded, we
// PROMOTE that element by flipping which slot is visible — a frame-clean
// hot-swap with no fresh load() on the visible path. Any other `src` change (a
// cold seek, a regen) falls back to loading the new source into the visible
// element. Only the active element plays, carries audio, owns `object-contain`,
// and is exposed via the parent `videoRef` (used for Director region capture).
export function VideoPlayer({
  videoRef,
  src,
  preloadSrc,
  bridging,
  bridgeKeyframeUrl,
  seekNonce,
  seekToS,
  playing,
  onTogglePlay,
  onTime,
  onEnded,
  bridgeSeed,
}: VideoPlayerProps) {
  const slot0Ref = useRef<HTMLVideoElement>(null);
  const slot1Ref = useRef<HTMLVideoElement>(null);
  const slotRefs = [slot0Ref, slot1Ref] as const;
  // The URL actually loaded into each slot — lets us avoid re-load()ing an
  // element that is already buffering the right clip (that's the whole point).
  const loadedSrc = useRef<[string | null, string | null]>([null, null]);
  const [activeSlot, setActiveSlot] = useState<0 | 1>(0);

  const loadInto = (slot: 0 | 1, url: string | null) => {
    const el = slotRefs[slot].current;
    if (!el || loadedSrc.current[slot] === url) return;
    loadedSrc.current[slot] = url;
    if (url) el.src = url;
    else el.removeAttribute("src");
    el.load();
  };

  // Reconcile the two slots against the engine's (src, preloadSrc).
  useEffect(() => {
    const inactive: 0 | 1 = activeSlot === 0 ? 1 : 0;
    const activeUrl = loadedSrc.current[activeSlot];
    const inactiveUrl = loadedSrc.current[inactive];

    if (src && inactiveUrl === src && activeUrl !== src) {
      // The buffer already holds this clip → promote it. Flip first; the next
      // pass re-tasks the demoted element as the new buffer, so the still-
      // visible element's src is never repointed (no flash).
      setActiveSlot(inactive);
      return;
    }
    loadInto(activeSlot, src);
    loadInto(inactive, preloadSrc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, preloadSrc, activeSlot]);

  // Keep the parent ref (Director region capture, §5.4) on the visible element.
  // The prop is a RefObject (readonly `current` in the types) but React mutates
  // it the same way, so the cast is safe and localized.
  useEffect(() => {
    (videoRef as MutableRefObject<HTMLVideoElement | null>).current =
      slotRefs[activeSlot].current;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot]);

  // Only the visible element plays and is unmuted; the buffer stays paused/muted
  // so promoting it is silent until it becomes visible.
  useEffect(() => {
    const inactive: 0 | 1 = activeSlot === 0 ? 1 : 0;
    const active = slotRefs[activeSlot].current;
    const buffer = slotRefs[inactive].current;
    if (buffer) {
      buffer.muted = true;
      buffer.pause();
    }
    if (active) {
      active.muted = false;
      if (playing && loadedSrc.current[activeSlot]) void active.play().catch(() => undefined);
      else active.pause();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, activeSlot, src]);

  // Seek the visible element when the engine bumps the nonce.
  useEffect(() => {
    const video = slotRefs[activeSlot].current;
    if (!video) return;
    const apply = () => {
      try {
        video.currentTime = seekToS;
      } catch {
        // metadata not ready yet — the listener below retries
      }
    };
    if (video.readyState >= 1) apply();
    else video.addEventListener("loadedmetadata", apply, { once: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seekNonce, seekToS, activeSlot]);

  const showBridge = bridging || !src;

  const slotClass = (slot: 0 | 1) =>
    `absolute inset-0 h-full w-full transition-opacity duration-150 ${
      slot === activeSlot ? "object-contain opacity-100" : "opacity-0 pointer-events-none"
    }`;

  return (
    <div className="relative aspect-video w-full overflow-hidden rounded-2xl bg-black ring-1 ring-white/10">
      <video
        ref={slot0Ref}
        crossOrigin="anonymous"
        playsInline
        preload="auto"
        className={slotClass(0)}
        onTimeUpdate={(e) => {
          if (activeSlot === 0) onTime(e.currentTarget.currentTime);
        }}
        onEnded={() => {
          if (activeSlot === 0) onEnded();
        }}
      />
      <video
        ref={slot1Ref}
        crossOrigin="anonymous"
        playsInline
        preload="auto"
        className={slotClass(1)}
        onTimeUpdate={(e) => {
          if (activeSlot === 1) onTime(e.currentTarget.currentTime);
        }}
        onEnded={() => {
          if (activeSlot === 1) onEnded();
        }}
      />

      {/* Ken-Burns bridge: shows the keyframe under a slow pan while real video
          renders. Fades out once the clip is playing (kinora.md §4.4 / §4.8). */}
      <div
        aria-hidden={!showBridge}
        className={`pointer-events-none absolute inset-0 transition-opacity duration-700 ${
          showBridge ? "opacity-100" : "opacity-0"
        }`}
      >
        {bridgeKeyframeUrl ? (
          <img
            src={bridgeKeyframeUrl}
            alt=""
            className="ken-burns-bridge h-full w-full object-cover"
            style={kenBurnsStyle(bridgeSeed ?? bridgeKeyframeUrl)}
          />
        ) : (
          <div
            className="ken-burns-bridge h-full w-full"
            style={{
              ...kenBurnsStyle(bridgeSeed ?? "kinora"),
              background:
                "radial-gradient(120% 90% at 30% 20%, rgba(124,92,255,0.28), transparent 60%), linear-gradient(180deg, #1b1b2b, #0b0b12)",
            }}
          />
        )}
        {showBridge ? (
          <span className="absolute bottom-3 left-3 rounded-full bg-black/55 px-2.5 py-1 text-[0.7rem] font-medium text-white/80 backdrop-blur">
            establishing…
          </span>
        ) : null}
      </div>

      {/* Play / pause affordance. */}
      <button
        type="button"
        onClick={onTogglePlay}
        aria-label={playing ? "Pause" : "Play"}
        className="group absolute inset-0 flex items-center justify-center"
      >
        <span
          className={`flex h-16 w-16 items-center justify-center rounded-full border border-white/40 bg-black/40 text-2xl text-white backdrop-blur-sm transition-opacity ${
            playing ? "opacity-0 group-hover:opacity-100" : "opacity-100"
          }`}
        >
          {playing ? <PauseIcon className="h-6 w-6" /> : <PlayIcon className="h-6 w-6" />}
        </span>
      </button>
    </div>
  );
}
