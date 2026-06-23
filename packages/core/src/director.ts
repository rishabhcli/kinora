/**
 * Director-mode shared logic (kinora.md §5.4), framework-agnostic so both shells
 * reuse it: the shot-timeline view-model (the book's shots merged with live
 * clip/QA/regen state, scene-windowed) and the region-select geometry (mapping a
 * pixel box drawn over an `object-contain` video to normalized content
 * coordinates). Pure — exhaustively unit-testable, no DOM.
 */
import type { ShotResponse } from "./api/types";
import { summarizeQa, type QaSummary } from "./feed";

// --- Shot timeline view-model ---------------------------------------------- #

/** Per-shot state the client layers over the fetched list (from §5.6 events). */
export interface ShotUpdate {
  clipUrl?: string | null;
  qa?: Record<string, unknown> | null;
  /** "regenerating" = a regen is in flight (optimistic); "ready" = a clip landed. */
  status?: "regenerating" | "ready";
}

export type ShotUpdateMap = Record<string, ShotUpdate>;

/** What a tile renders: pending (no clip), regenerating, or ready (has a clip). */
export type ShotTileStatus = "pending" | "regenerating" | "ready";

export interface DirectorShot {
  shotId: string;
  sceneId: string | null;
  beatId: string | null;
  /** 1-based position within the shot's scene (for the tile label). */
  sceneIndex: number;
  startWord: number;
  endWord: number;
  page: number;
  durationS: number;
  clipUrl: string | null;
  qa: QaSummary | null;
  status: ShotTileStatus;
}

function wordRange(span: ShotResponse["source_span"]): [number, number] {
  const raw = (span as { word_range?: unknown } | null)?.word_range;
  if (
    Array.isArray(raw) &&
    raw.length === 2 &&
    typeof raw[0] === "number" &&
    typeof raw[1] === "number"
  ) {
    return [raw[0], raw[1]];
  }
  return [0, 0];
}

function pageOf(span: ShotResponse["source_span"]): number {
  const page = (span as { page?: unknown } | null)?.page;
  return typeof page === "number" ? page : 0;
}

/** Merge the fetched shots with live updates into ordered, scene-numbered tiles. */
export function toDirectorShots(
  shots: readonly ShotResponse[],
  updates: ShotUpdateMap = {},
): DirectorShot[] {
  const sceneCounts = new Map<string, number>();
  return shots.map((shot) => {
    const update = updates[shot.shot_id];
    const clipUrl = update?.clipUrl !== undefined ? update.clipUrl : (shot.clip_url ?? null);
    const qa = update?.qa !== undefined ? update.qa : (shot.qa ?? null);
    const [startWord, endWord] = wordRange(shot.source_span);
    const sceneKey = shot.scene_id ?? "_";
    const sceneIndex = (sceneCounts.get(sceneKey) ?? 0) + 1;
    sceneCounts.set(sceneKey, sceneIndex);

    const status: ShotTileStatus =
      update?.status === "regenerating" ? "regenerating" : clipUrl ? "ready" : "pending";

    return {
      shotId: shot.shot_id,
      sceneId: shot.scene_id ?? null,
      beatId: shot.beat_id ?? null,
      sceneIndex,
      startWord,
      endWord,
      page: pageOf(shot.source_span),
      durationS: shot.duration_s ?? 0,
      clipUrl,
      qa: summarizeQa(qa),
      status,
    };
  });
}

/**
 * The filmstrip window: the shots in the same scene as the one on screen. Falls
 * back to the whole book when no shot is current yet (nothing selected).
 */
export function sceneWindow(
  shots: readonly DirectorShot[],
  currentShotId: string | null,
): DirectorShot[] {
  if (!currentShotId) return [...shots];
  const current = shots.find((s) => s.shotId === currentShotId);
  if (!current || current.sceneId === null) return [...shots];
  return shots.filter((s) => s.sceneId === current.sceneId);
}

// --- Region-select geometry (§5.4 pointer commenting) ---------------------- #

/** A box in normalized [0,1] coordinates over the video's *content* rectangle. */
export interface NormBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** A box in pixels, relative to the displayed video element's top-left. */
export interface PixelBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

const clamp01 = (n: number): number => Math.min(1, Math.max(0, n));

/**
 * Map a pixel box drawn over a displayed (`object-contain`) video to normalized
 * coordinates over the real video content. `rectW/rectH` is the element's
 * displayed size, `videoW/videoH` its intrinsic size. Returns `null` when the
 * video has no intrinsic size yet or the box collapses to nothing.
 */
export function contentNormFromPixels(
  rectW: number,
  rectH: number,
  videoW: number,
  videoH: number,
  box: PixelBox,
): NormBox | null {
  if (!videoW || !videoH || rectW === 0 || rectH === 0) return null;

  // object-contain: the content scales by the limiting dimension and is centered,
  // leaving symmetric letterbox/pillarbox bars we must subtract before normalizing.
  const scale = Math.min(rectW / videoW, rectH / videoH);
  const contentW = videoW * scale;
  const contentH = videoH * scale;
  const offX = (rectW - contentW) / 2;
  const offY = (rectH - contentH) / 2;

  const x0 = clamp01((box.x - offX) / contentW);
  const y0 = clamp01((box.y - offY) / contentH);
  const x1 = clamp01((box.x + box.w - offX) / contentW);
  const y1 = clamp01((box.y + box.h - offY) / contentH);

  const x = Math.min(x0, x1);
  const y = Math.min(y0, y1);
  const w = Math.abs(x1 - x0);
  const h = Math.abs(y1 - y0);
  if (w < 0.012 || h < 0.012) return null; // ignore a stray click / hairline drag
  return { x, y, w, h };
}

/** A box as CSS percentages of the displayed element (for positioning a marker). */
export interface ElementPctRect {
  leftPct: number;
  topPct: number;
  widthPct: number;
  heightPct: number;
}

/**
 * Inverse of {@link contentNormFromPixels}: place a normalized content box back
 * onto the displayed (`object-contain`) element as CSS percentages, re-adding the
 * letterbox/pillarbox offset. Lets a persistent region marker sit exactly over
 * the boxed subject for *any* clip aspect, not just one that fills the stage.
 */
export function contentNormToElementRect(
  rectW: number,
  rectH: number,
  videoW: number,
  videoH: number,
  box: NormBox,
): ElementPctRect | null {
  if (!videoW || !videoH || rectW === 0 || rectH === 0) return null;
  const scale = Math.min(rectW / videoW, rectH / videoH);
  const contentW = videoW * scale;
  const contentH = videoH * scale;
  const offX = (rectW - contentW) / 2;
  const offY = (rectH - contentH) / 2;
  return {
    leftPct: ((offX + box.x * contentW) / rectW) * 100,
    topPct: ((offY + box.y * contentH) / rectH) * 100,
    widthPct: ((box.w * contentW) / rectW) * 100,
    heightPct: ((box.h * contentH) / rectH) * 100,
  };
}
