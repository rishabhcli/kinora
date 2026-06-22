import { type RefObject, useEffect, useRef } from "react";

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
  const preloadRef = useRef<HTMLVideoElement>(null);

  // Load a new source, then honour the desired play/pause state.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !src) return;
    video.load();
    if (playing) void video.play().catch(() => undefined);
  }, [src, videoRef]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !src) return;
    if (playing) void video.play().catch(() => undefined);
    else video.pause();
  }, [playing, src, videoRef]);

  // Seek when the engine bumps the nonce (scroll-driven or explicit seek).
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const apply = () => {
      try {
        video.currentTime = seekToS;
      } catch {
        // metadata not ready yet — the listener below will retry
      }
    };
    if (video.readyState >= 1) apply();
    else {
      video.addEventListener("loadedmetadata", apply, { once: true });
    }
  }, [seekNonce, seekToS, videoRef]);

  const showBridge = bridging || !src;

  return (
    <div className="relative aspect-video w-full overflow-hidden rounded-2xl bg-black ring-1 ring-white/10">
      {src ? (
        <video
          ref={videoRef}
          src={src}
          crossOrigin="anonymous"
          playsInline
          className="h-full w-full object-contain"
          onTimeUpdate={(e) => onTime(e.currentTarget.currentTime)}
          onEnded={onEnded}
        />
      ) : null}

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

      {/* Hidden preload buffer — warms the next clip for a seamless hot-swap. */}
      {preloadSrc ? (
        <video ref={preloadRef} src={preloadSrc} preload="auto" muted className="hidden" />
      ) : null}
    </div>
  );
}
