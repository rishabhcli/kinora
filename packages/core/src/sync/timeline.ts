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
