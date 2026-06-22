import type { Bbox, Shot, SyncMap, SyncSegment, SyncWord } from "../api/types";

// The sync map (kinora.md §9.4) binds video-time ↔ page ↔ word. CosyVoice
// word timestamps power three features from one artifact: the karaoke
// highlight, the auto page-turn, and the scroll→video seek. These helpers are
// pure so the exact mapping can be unit-tested without a video element.

/** Absolute video time → segment-local time. */
export function localTime(segment: SyncSegment, videoTimeS: number): number {
  return videoTimeS - segment.video_start_s;
}

/**
 * The word being spoken at a given segment-local time. Returns the last word
 * whose `t_start <= localS`; during the small gap before the next word starts
 * we keep the current word lit (standard karaoke behaviour). Returns null
 * before the first word, or after the final word has finished.
 */
export function activeWordIndexAt(words: SyncWord[], localS: number): number | null {
  if (words.length === 0) return null;
  let lo = 0;
  let hi = words.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (words[mid].t_start <= localS) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  if (ans < 0) return null;
  if (ans < words.length - 1) return words[ans].word_index; // gap → keep current
  return localS <= words[ans].t_end ? words[ans].word_index : null;
}

export type WordHighlightState = "played" | "active" | "ahead";

/** Karaoke state of a single word at a segment-local time. */
export function highlightStateForWord(word: SyncWord, localS: number): WordHighlightState {
  if (localS >= word.t_end) return "played";
  if (localS >= word.t_start) return "active";
  return "ahead";
}

export function bboxForWord(words: SyncWord[], wordIndex: number): Bbox | null {
  const w = words.find((x) => x.word_index === wordIndex);
  return w?.bbox ?? null;
}

/** §9.4 — flip the page slightly before the shot ends. */
export function shouldTurnPage(segment: SyncSegment, videoTimeS: number): boolean {
  return videoTimeS >= segment.page_turn_at_s;
}

export interface SeekTarget {
  shotId: string;
  /** Absolute video time within the (per-shot or stitched) clip. */
  videoTimeS: number;
  segment: SyncSegment;
}

/**
 * Scroll→video: resolve a focus word to a shot and an in-shot timestamp via
 * the sync map. Picks the segment whose word range contains `w`, then the
 * word at-or-just-before `w`, and returns its absolute start time.
 */
export function seekTargetForWord(map: SyncMap, w: number): SeekTarget | null {
  for (const segment of map.segments) {
    const words = segment.words;
    if (words.length === 0) continue;
    const first = words[0].word_index;
    const last = words[words.length - 1].word_index;
    if (w < first || w > last) continue;
    let chosen: SyncWord = words[0];
    for (const word of words) {
      if (word.word_index <= w) chosen = word;
      else break;
    }
    return {
      shotId: segment.shot_id,
      videoTimeS: segment.video_start_s + chosen.t_start,
      segment,
    };
  }
  return null;
}

/**
 * Resolve a focus word to a shot via the source-span index (kinora.md §4.2).
 * Shots are assumed sorted by word_range start; uses binary search → O(log n).
 * Returns the index into `shots`, or null if `w` precedes the first shot.
 */
export function shotIndexForWord(shots: Shot[], w: number): number | null {
  if (shots.length === 0) return null;
  let lo = 0;
  let hi = shots.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (shots[mid].source_span.word_range[0] <= w) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans < 0 ? null : ans;
}

export function shotForWord(shots: Shot[], w: number): Shot | null {
  const idx = shotIndexForWord(shots, w);
  return idx === null ? null : shots[idx];
}
