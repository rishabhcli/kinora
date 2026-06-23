/**
 * Pure helpers that turn the backend's shot list + per-shot sync segments into
 * the data the SyncEngine needs: a word-ordered timeline (so a reading position
 * resolves to a shot), and per-clip lookups for the karaoke highlight and the
 * page-turn. No state, no I/O — exhaustively unit-testable.
 */
import type { ShotResponse } from "../api/types";
import type { SyncSegment } from "../events";

export interface TimelineShot {
  shotId: string;
  /** The beat this shot dramatizes — keys the §4.4 keyframe (one still per beat). */
  beatId: string | null;
  /** The scene this shot belongs to (the stitch boundary, §4.2), or null. */
  sceneId: string | null;
  /** Inclusive source word range this shot covers. */
  startWord: number;
  endWord: number;
  page: number;
  durationS: number;
  /** Cumulative start offset on the global playhead (sum of prior durations). */
  videoStartS: number;
  clipUrl: string | null;
  status: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

function spanRange(span: unknown): [number, number] | null {
  const raw = asRecord(span)?.["word_range"];
  if (!Array.isArray(raw) || raw.length !== 2) return null;
  const start = raw[0];
  const end = raw[1];
  return typeof start === "number" && typeof end === "number" ? [start, end] : null;
}

function spanPage(span: unknown): number {
  const page = asRecord(span)?.["page"];
  return typeof page === "number" ? page : 0;
}

/** Build a word-ordered timeline with cumulative video offsets from raw shots. */
export function buildTimeline(shots: readonly ShotResponse[]): TimelineShot[] {
  const entries = shots.map((shot) => {
    const range = spanRange(shot.source_span);
    return {
      shotId: shot.shot_id,
      beatId: shot.beat_id ?? null,
      sceneId: shot.scene_id ?? null,
      startWord: range ? range[0] : 0,
      endWord: range ? range[1] : 0,
      page: spanPage(shot.source_span),
      durationS: shot.duration_s ?? 0,
      clipUrl: shot.clip_url ?? null,
      status: shot.status,
    };
  });
  entries.sort((a, b) => a.startWord - b.startWord || a.endWord - b.endWord);

  let offset = 0;
  return entries.map((entry) => {
    const withOffset: TimelineShot = { ...entry, videoStartS: offset };
    offset += entry.durationS;
    return withOffset;
  });
}

/** Index of the shot covering `word`, else the last shot starting at/before it, else -1. */
export function shotIndexForWord(timeline: readonly TimelineShot[], word: number): number {
  let candidate = -1;
  for (let i = 0; i < timeline.length; i++) {
    const shot = timeline[i];
    if (shot === undefined) continue;
    if (word >= shot.startWord && word <= shot.endWord) return i;
    if (shot.startWord <= word) candidate = i;
    else break;
  }
  return candidate;
}

/** Index into `segment.words` of the word being narrated at clip time `tSec` (-1 if before). */
export function activeSyncWordIndexAt(segment: SyncSegment, tSec: number): number {
  let idx = -1;
  for (let i = 0; i < segment.words.length; i++) {
    const word = segment.words[i];
    if (word === undefined) continue;
    if (tSec >= word.t_start) idx = i;
    else break;
  }
  return idx;
}

/** The global `word_index` highlighted at clip time `tSec`, or null. */
export function highlightedWordIndexAt(segment: SyncSegment, tSec: number): number | null {
  const i = activeSyncWordIndexAt(segment, tSec);
  const word = i >= 0 ? segment.words[i] : undefined;
  return word ? word.word_index : null;
}

/** Whether the page should already have flipped by clip time `tSec` (§9.4 lead). */
export function shouldTurnPage(segment: SyncSegment, tSec: number): boolean {
  return tSec >= segment.page_turn_at_s;
}

// --- Scene-level (stitched) timeline (§9.6) -------------------------------- #

/**
 * One shot's segment inside a stitched scene. It is a {@link SyncSegment} whose
 * `video_start_s`/`video_end_s` and word `t_start`/`t_end` are in **absolute
 * scene time** (the backend's `merge_sync_segments` shifted them onto the scene
 * timeline), annotated with the shot's source word range so a reading position
 * resolves to the right segment.
 */
export interface SceneSegment extends SyncSegment {
  /** Inclusive source word range this segment (shot) covers. */
  startWord: number;
  endWord: number;
}

/**
 * A scene that has been stitched into one continuous film (`scene_stitched`):
 * a single clip URL whose segments play back-to-back in absolute time. Once a
 * scene is stitched the engine prefers it over the per-shot clips for the
 * scene's word range, so reading a whole scene is one gapless asset.
 */
export interface StitchedScene {
  sceneId: string;
  /** The stitched scene mp4 (one asset for the whole scene). */
  clipUrl: string;
  /** Total scene length in seconds (the last segment's `video_end_s`). */
  durationS: number;
  /** Inclusive source word range the stitched asset covers. */
  startWord: number;
  endWord: number;
  /** Segments in absolute scene time, ordered by `video_start_s`. */
  segments: SceneSegment[];
}

/** The source word range a scene segment covers — shot span first, else its words. */
function segmentWordRange(segment: SyncSegment, shot: TimelineShot | undefined): [number, number] {
  if (shot) return [shot.startWord, shot.endWord];
  if (segment.words.length > 0) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const word of segment.words) {
      if (word.word_index < lo) lo = word.word_index;
      if (word.word_index > hi) hi = word.word_index;
    }
    return [lo, hi];
  }
  return [Number.NaN, Number.NaN];
}

/**
 * Build a {@link StitchedScene} from a `scene_stitched` payload + the shot
 * timeline. The scene's word range is the union of its segments' source spans
 * (looked up by `shot_id`); returns `null` when there is nothing playable.
 */
export function buildStitchedScene(
  sceneId: string,
  clipUrl: string | null | undefined,
  segments: readonly SyncSegment[],
  timeline: readonly TimelineShot[],
): StitchedScene | null {
  if (!clipUrl || segments.length === 0) return null;
  const byShot = new Map(timeline.map((shot) => [shot.shotId, shot] as const));

  const built: SceneSegment[] = segments
    .map((segment) => {
      const [startWord, endWord] = segmentWordRange(segment, byShot.get(segment.shot_id));
      return { ...segment, startWord, endWord };
    })
    .sort((a, b) => a.video_start_s - b.video_start_s);

  let startWord = Infinity;
  let endWord = -Infinity;
  let durationS = 0;
  for (const segment of built) {
    if (Number.isFinite(segment.startWord)) startWord = Math.min(startWord, segment.startWord);
    if (Number.isFinite(segment.endWord)) endWord = Math.max(endWord, segment.endWord);
    durationS = Math.max(durationS, segment.video_end_s);
  }
  if (!Number.isFinite(startWord) || !Number.isFinite(endWord)) return null;

  return { sceneId, clipUrl, durationS, startWord, endWord, segments: built };
}

/** Index of the segment covering scene-absolute time `tSec`, else the last one starting at/before it, else -1. */
export function segmentIndexAtTime(scene: StitchedScene, tSec: number): number {
  let candidate = -1;
  for (let i = 0; i < scene.segments.length; i++) {
    const segment = scene.segments[i];
    if (segment === undefined) continue;
    if (tSec >= segment.video_start_s && tSec < segment.video_end_s) return i;
    if (segment.video_start_s <= tSec) candidate = i;
    else break;
  }
  return candidate;
}

/** Index of the segment whose source words cover `word`, else the last one at/before it, else -1. */
export function segmentIndexForWord(scene: StitchedScene, word: number): number {
  let candidate = -1;
  for (let i = 0; i < scene.segments.length; i++) {
    const segment = scene.segments[i];
    if (segment === undefined) continue;
    if (word >= segment.startWord && word <= segment.endWord) return i;
    if (segment.startWord <= word) candidate = i;
    else break;
  }
  return candidate;
}

/**
 * Map a global `word` to its absolute time in the stitched scene asset: the
 * `t_start` of the latest narrated word at/before it, else the start of the
 * segment that contains it. This is what a mid-scene seek (§4.8) lands on.
 */
export function sceneTimeForWord(scene: StitchedScene, word: number): number {
  const index = segmentIndexForWord(scene, word);
  const segment = index >= 0 ? scene.segments[index] : undefined;
  if (!segment) return 0;
  let tSec = segment.video_start_s;
  for (const w of segment.words) {
    if (w.word_index <= word) tSec = w.t_start;
    else break;
  }
  return tSec;
}
