// Frame-accurate scrubbing math, pure half. When the reader drags the playhead we
// currently push `currentTime` at ~30Hz (FilmPane.SCRUB_SEEK_INTERVAL_MS); that's
// smooth while moving but, when the drag STOPS, the video can settle on a time
// that sits between two frames, so the same scroll position can show slightly
// different frames run-to-run (inter-frame shimmer). Frame-accurate scrubbing
// quantises the target time to the centre of a discrete frame so a paused scrub
// always lands on one stable, repeatable frame — the behaviour of a real NLE
// scrubber.
//
// DOM-free: feed it fps + clip duration + a target time (or a 0..1 position); it
// returns the frame index and the quantised time to seek to. The engine's seek
// adapter calls this only when settling (mode flips scrub→play / pointer up), so
// live dragging keeps its cheap continuous seeks.

export interface FrameClockConfig {
  /** frames per second of the clip (e.g. 24, 25, 30) */
  fps: number;
  /** total clip duration in seconds (the [clipStart,clipEnd] window length) */
  durationS: number;
}

export interface FrameInfo {
  /** zero-based frame index within the clip */
  index: number;
  /** total frame count (floor(duration*fps), at least 1) */
  total: number;
  /** the seconds to seek to — the CENTRE of the frame's display interval */
  timeS: number;
}

/** Total whole frames in a clip (at least 1 so an unknown-duration clip is sane). */
export function frameCount(config: FrameClockConfig): number {
  if (!(config.fps > 0) || !(config.durationS > 0)) return 1;
  return Math.max(1, Math.floor(config.durationS * config.fps));
}

const clamp = (v: number, lo: number, hi: number): number => (v < lo ? lo : v > hi ? hi : v);

/** Quantise a target *time* (s) to the nearest frame, returning the frame index
 *  and the seek time at the frame's centre. Centre-of-frame (index+0.5)/fps avoids
 *  landing exactly on a boundary, where decoders disagree on which frame to show. */
export function quantizeTime(config: FrameClockConfig, timeS: number): FrameInfo {
  const total = frameCount(config);
  if (!(config.fps > 0)) {
    // Unknown fps: can't quantise — pass the time through as a single "frame".
    const t = clamp(timeS, 0, config.durationS > 0 ? config.durationS : timeS);
    return { index: 0, total: 1, timeS: t };
  }
  const raw = Math.floor(timeS * config.fps);
  const index = clamp(raw, 0, total - 1);
  const centre = (index + 0.5) / config.fps;
  // Never seek past the clip's end (the last partial frame).
  const maxT = config.durationS > 0 ? config.durationS : centre;
  return { index, total, timeS: Math.min(centre, maxT) };
}

/** Quantise a 0..1 *position* across the clip to a frame. */
export function quantizePosition(config: FrameClockConfig, position: number): FrameInfo {
  const total = frameCount(config);
  const p = clamp(position, 0, 1);
  // Map [0,1] across `total` frames; the last position maps to the last frame.
  const index = Math.min(total - 1, Math.floor(p * total));
  if (!(config.fps > 0)) {
    return { index: 0, total: 1, timeS: p * (config.durationS || 0) };
  }
  const centre = (index + 0.5) / config.fps;
  const maxT = config.durationS > 0 ? config.durationS : centre;
  return { index, total, timeS: Math.min(centre, maxT) };
}

/** Step `delta` frames from a current time, clamped to the clip. For arrow-key
 *  frame stepping (accessibility) and the next/prev-frame scrub controls. */
export function stepFrames(config: FrameClockConfig, currentTimeS: number, delta: number): FrameInfo {
  const here = quantizeTime(config, currentTimeS);
  const total = here.total;
  const index = clamp(here.index + Math.trunc(delta), 0, total - 1);
  if (!(config.fps > 0)) return { index: 0, total: 1, timeS: currentTimeS };
  const centre = (index + 0.5) / config.fps;
  const maxT = config.durationS > 0 ? config.durationS : centre;
  return { index, total, timeS: Math.min(centre, maxT) };
}

/** Is `a` the same frame as `b` for this clock? (Used to skip redundant seeks.) */
export function sameFrame(config: FrameClockConfig, a: number, b: number): boolean {
  if (!(config.fps > 0)) return Math.abs(a - b) < 1e-4;
  return quantizeTime(config, a).index === quantizeTime(config, b).index;
}
