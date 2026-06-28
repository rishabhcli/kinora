// A thin wrapper over HTMLVideoElement.requestVideoFrameCallback (rVFC, Chromium /
// Electron) with a requestAnimationFrame fallback. rVFC fires once per *presented
// video frame* with rich metadata (presentation timestamp, expected display time,
// processing duration, mediaTime) — exactly what frame-accurate scrubbing wants to
// (a) confirm a seek landed on the intended frame and (b) measure real decode/
// present timing for the perf panel. When rVFC is unavailable we fall back to rAF
// and synthesise the `mediaTime` from `currentTime`, so callers get a uniform API.
//
// The DOM touch is unavoidable here; we keep it tiny and structural so a test can
// drive it with a stub video. Pure frame math lives in frameClock.ts.

export interface PresentedFrame {
  /** the video's media time (s) of the presented frame */
  mediaTime: number;
  /** monotonic presentation timestamp (ms), from rVFC or performance.now() */
  presentationTime: number;
  /** count of frames presented since the last callback (rVFC `presentedFrames`) */
  presentedFrames: number;
  /** processing duration in seconds, if the platform reports it */
  processingDuration?: number;
  /** true when this came from the real rVFC (vs the rAF fallback) */
  precise: boolean;
}

/** The structural slice of HTMLVideoElement we use — lets tests pass a stub. */
export interface FrameCallbackVideo {
  currentTime: number;
  requestVideoFrameCallback?: (cb: (now: number, meta: VideoFrameMetadataLike) => void) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
}

interface VideoFrameMetadataLike {
  mediaTime: number;
  presentationTime?: number;
  presentedFrames?: number;
  processingDuration?: number;
}

type Raf = (cb: (t: number) => void) => number;
type Caf = (h: number) => void;

export interface FrameWatcherOptions {
  /** inject rAF/caf + clock for tests; default to the globals */
  raf?: Raf;
  caf?: Caf;
  now?: () => number;
}

/** Subscribe to presented frames of `video`. Returns an unsubscribe fn. Uses rVFC
 *  when present, else a rAF loop that reports the current media time each frame. */
export function watchPresentedFrames(
  video: FrameCallbackVideo,
  onFrame: (frame: PresentedFrame) => void,
  options: FrameWatcherOptions = {},
): () => void {
  const raf: Raf =
    options.raf ?? (typeof requestAnimationFrame === "function" ? requestAnimationFrame.bind(globalThis) : null) ?? noopRaf;
  const caf: Caf = options.caf ?? (typeof cancelAnimationFrame === "function" ? cancelAnimationFrame.bind(globalThis) : () => {});
  const now = options.now ?? (typeof performance !== "undefined" ? () => performance.now() : () => Date.now());

  let stopped = false;
  let handle = 0;

  if (typeof video.requestVideoFrameCallback === "function") {
    const cancel = video.cancelVideoFrameCallback?.bind(video);
    const tick = (presentNow: number, meta: VideoFrameMetadataLike) => {
      if (stopped) return;
      onFrame({
        mediaTime: meta.mediaTime,
        presentationTime: meta.presentationTime ?? presentNow,
        presentedFrames: meta.presentedFrames ?? 1,
        processingDuration: meta.processingDuration,
        precise: true,
      });
      handle = video.requestVideoFrameCallback!(tick);
    };
    handle = video.requestVideoFrameCallback(tick);
    return () => {
      stopped = true;
      cancel?.(handle);
    };
  }

  // Fallback: rAF, synthesising mediaTime from currentTime. Lower fidelity but a
  // uniform API (precise=false lets the caller relax its assertions).
  const loop = () => {
    if (stopped) return;
    onFrame({
      mediaTime: video.currentTime,
      presentationTime: now(),
      presentedFrames: 1,
      precise: false,
    });
    handle = raf(loop);
  };
  handle = raf(loop);
  return () => {
    stopped = true;
    caf(handle);
  };
}

/** True if the platform exposes the precise rVFC API on this video. */
export function hasFrameCallback(video: FrameCallbackVideo): boolean {
  return typeof video.requestVideoFrameCallback === "function";
}

function noopRaf(): number {
  return 0;
}
