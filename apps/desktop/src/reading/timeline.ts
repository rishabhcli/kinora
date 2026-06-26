// The sync-map / scrubbing math for the Scroll Film Engine (Agent 02).
//
// Pure, DOM-free, and decoupled from `lib/api.ts` (Agent 12's surface) so it is
// unit-testable on its own (see `__tests__/timeline.test.ts`). The engine and
// hook adapt backend shapes (`ShotResponse`, the SSE clip map, Agent 1's stitched
// event films) into `SegmentInput[]` and feed them here.
//
// The model: a reader scrolling a book is scrubbing one continuous film. We map
//   scroll fraction → focus word → segment + local fraction → video currentTime.
// A `Timeline` is an ordered, contiguous list of `FilmSegment`s; each maps a
// global word range onto a [clipStart, clipEnd] window inside its source mp4.
// Consecutive segments that share a `src` are one stitched event film (scrubbing
// across them is a pure `currentTime` change — no crossfade); a `src` change is
// an event/shot boundary that the FilmPane crossfades (WS2).

/** One unit handed to {@link buildTimeline}. `clipStart`/`clipEnd` are seconds
 *  inside `src`; when omitted they default to `[0, duration ?? 0]`. A zero-length
 *  window (e.g. the single bundled fallback film, whose duration isn't known
 *  until `loadedmetadata`) signals "use the live <video> duration at runtime". */
export interface SegmentInput {
  id: string;
  wordStart: number;
  wordEnd: number;
  src: string;
  clipStart?: number;
  clipEnd?: number;
  duration?: number | null;
}

export interface FilmSegment {
  id: string;
  src: string;
  /** global word index where this segment starts (inclusive) */
  wordStart: number;
  /** global word index where this segment ends (exclusive) */
  wordEnd: number;
  /** seconds into `src` where this segment begins */
  clipStart: number;
  /** seconds into `src` where this segment ends (=== clipStart ⇒ unknown) */
  clipEnd: number;
}

export interface Timeline {
  /** ordered by `wordStart`, contiguous (no dead zones while scrubbing) */
  segments: FilmSegment[];
  /** word index covered by the timeline (the last segment's `wordEnd`) */
  totalWords: number;
}

export interface PlayheadTarget {
  segment: FilmSegment;
  /** index of `segment` within `timeline.segments` */
  index: number;
  /** position within the segment's word span, clamped to [0, 1] */
  localFraction: number;
}

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

/** Build a contiguous timeline from segments (shots, stitched-event shots, or a
 *  single fallback film). Sorts by `wordStart` and absorbs any gap between
 *  consecutive segments into the earlier one, so scrubbing never lands in a dead
 *  zone with no film. */
export function buildTimeline(inputs: SegmentInput[]): Timeline {
  if (inputs.length === 0) return { segments: [], totalWords: 0 };

  const sorted = [...inputs].sort((a, b) => a.wordStart - b.wordStart);
  const segments: FilmSegment[] = sorted.map((s) => {
    const clipStart = s.clipStart ?? 0;
    const clipEnd = s.clipEnd ?? clipStart + (s.duration ?? 0);
    return { id: s.id, src: s.src, wordStart: s.wordStart, wordEnd: s.wordEnd, clipStart, clipEnd };
  });
  // Make word ranges contiguous: each segment runs up to the next one's start.
  for (let i = 0; i < segments.length - 1; i++) {
    segments[i].wordEnd = segments[i + 1].wordStart;
  }
  return { segments, totalWords: segments[segments.length - 1].wordEnd };
}

/** Resolve the playhead for a focus word: the greatest segment whose `wordStart`
 *  is ≤ the focus word (mirrors ReadingRoom's `activeShot` rule), plus where
 *  inside that segment the reader is. Returns `null` only for an empty timeline. */
export function resolvePlayhead(timeline: Timeline, focusWord: number): PlayheadTarget | null {
  const { segments } = timeline;
  if (segments.length === 0) return null;
  let index = 0;
  for (let i = 0; i < segments.length; i++) {
    if (segments[i].wordStart <= focusWord) index = i;
    else break;
  }
  const segment = segments[index];
  const span = segment.wordEnd - segment.wordStart;
  const localFraction = span > 0 ? clamp((focusWord - segment.wordStart) / span, 0, 1) : 0;
  return { segment, index, localFraction };
}

/** Scroll fraction (0..1) → global focus word. Mirrors ReadingRoom's
 *  `Math.round(frac * totalWords)`, clamped. */
export function focusWordFromFraction(fraction: number, totalWords: number): number {
  return Math.round(clamp(fraction, 0, 1) * totalWords);
}

/** The `currentTime` (seconds) to seek a segment's `<video>` to for a given
 *  local fraction. Uses the segment's known [clipStart, clipEnd] window; if that
 *  window is unknown (zero-length), falls back to `localFraction * liveDuration`
 *  (the bundled fallback film, sized only once metadata loads). */
export function segmentTime(
  segment: Pick<FilmSegment, "clipStart" | "clipEnd">,
  localFraction: number,
  liveDuration?: number,
): number {
  const span = segment.clipEnd - segment.clipStart;
  if (span > 0) return segment.clipStart + localFraction * span;
  if (liveDuration && liveDuration > 0) return localFraction * liveDuration;
  return segment.clipStart;
}

export interface ScrollClassifyOpts {
  /** words/sec at or above which we scrub instead of play (default 16) */
  scrubThreshold?: number;
}

/** Velocity-aware mode: a fast flick scrubs the timeline; slow reading / at-rest
 *  lets the film play forward. Direction-agnostic. */
export function classifyScroll(
  velocityWordsPerSec: number,
  opts: ScrollClassifyOpts = {},
): "scrub" | "play" {
  const threshold = opts.scrubThreshold ?? 16;
  return Math.abs(velocityWordsPerSec) >= threshold ? "scrub" : "play";
}

export interface SchedulerSignal {
  /** `seek` cancels distant speculative work on a big jump; `intent` nudges the
   *  buffer window for normal reading. */
  kind: "seek" | "intent";
  word: number;
  /** words/sec, clamped to [2, 12] (the scheduler's expected range) */
  velocity: number;
}

/** Reproduce ReadingRoom's scheduler signalling (lines ~197–204): a jump of more
 *  than 120 words is a `seek`; otherwise post `intent` with a clamped velocity.
 *  `dtSeconds` ≤ 0 yields the default reading velocity of 4. */
export function schedulerSignal(prevWord: number, word: number, dtSeconds: number): SchedulerSignal {
  const dw = word - prevWord;
  const velocity = dtSeconds > 0 ? clamp(Math.abs(dw) / dtSeconds, 2, 12) : 4;
  return { kind: Math.abs(dw) > 120 ? "seek" : "intent", word, velocity };
}

/** The next segment to decode ahead of the reader: the one immediately after the
 *  current playhead, but only once its start is within `lookaheadWords`. Returns
 *  `null` when the boundary is still far off or the reader is in the last
 *  segment. Coordinates with Agent 9 perf helpers (preload-on-idle). */
export function nextSegmentToPreload(
  timeline: Timeline,
  focusWord: number,
  lookaheadWords: number,
): FilmSegment | null {
  const head = resolvePlayhead(timeline, focusWord);
  if (!head) return null;
  const next = timeline.segments[head.index + 1];
  if (!next) return null;
  const distance = next.wordStart - focusWord;
  return distance >= 0 && distance <= lookaheadWords ? next : null;
}
