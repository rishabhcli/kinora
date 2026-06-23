import type { SyncEngine } from "@kinora/core";
import { useRef } from "react";
import { useEffect } from "react";

interface VideoStageProps {
  engine: SyncEngine;
  clipUrl: string | null;
}

/**
 * The film pane. Plays the current shot's clip and drives the playhead from real
 * frame callbacks (`requestVideoFrameCallback`, falling back to `timeupdate`),
 * so the karaoke highlight and page-turn stay frame-accurate. A clip-URL change
 * hot-swaps the source and restarts from 0.
 */
export function VideoStage({ engine, clipUrl }: VideoStageProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !clipUrl) return;

    video.src = clipUrl;
    video.currentTime = 0;
    void video.play().catch(() => {
      // Autoplay may be blocked until interaction; the controls let the user start.
    });

    let cancelled = false;
    let handle = 0;
    const hasRvfc = typeof video.requestVideoFrameCallback === "function";

    const onFrame = (): void => {
      if (cancelled) return;
      engine.reportVideoTime(video.currentTime, performance.now());
      if (hasRvfc) handle = video.requestVideoFrameCallback(onFrame);
    };
    const onTimeUpdate = (): void => {
      engine.reportVideoTime(video.currentTime, performance.now());
    };

    if (hasRvfc) {
      handle = video.requestVideoFrameCallback(onFrame);
    } else {
      video.addEventListener("timeupdate", onTimeUpdate);
    }

    return () => {
      cancelled = true;
      if (hasRvfc && handle) video.cancelVideoFrameCallback(handle);
      else video.removeEventListener("timeupdate", onTimeUpdate);
    };
  }, [engine, clipUrl]);

  return (
    <div className="flex h-full items-center justify-center bg-black">
      {clipUrl ? (
        <video
          ref={videoRef}
          className="max-h-full max-w-full"
          controls
          playsInline
          onPlay={() => engine.setPlaying(true)}
          onPause={() => engine.setPlaying(false)}
        />
      ) : (
        <p className="text-sm text-neutral-500">Waiting for the next shot…</p>
      )}
    </div>
  );
}
