/**
 * The SyncEngine — the client-side single source of truth for the playhead
 * (kinora.md §5.2). It binds scroll <-> video <-> word bidirectionally without a
 * feedback loop (ownership + a handoff grace window), tracks reading position
 * `w` and velocity `v`, pushes debounced intent to the Scheduler, and hot-swaps
 * clips as `clip_ready` events arrive.
 *
 * Framework-agnostic: state is exposed via `subscribe`/`getSnapshot` (for React's
 * `useSyncExternalStore` on both shells) and every time-sensitive entry point
 * takes an explicit `nowMs`, so the grace, debounce, and velocity are all
 * deterministically testable.
 */
import type { ShotResponse } from "../api/types";
import type { SyncSegment } from "../events";
import { LruMap } from "./lruMap";
import {
  buildStitchedScene,
  buildTimeline,
  highlightedWordIndexAt,
  sceneTimeForWord,
  segmentIndexAtTime,
  segmentIndexForWord,
  shotIndexForWord,
  shouldTurnPage,
  type StitchedScene,
  type TimelineShot,
} from "./timeline";
import { VelocityTracker, type VelocityOptions } from "./velocity";

export type ControlOwner = "scroll" | "video";
export type SessionMode = "viewer" | "director";
export type SourceKind = "shot" | "scene";

/** What the shell plays now (or preloads next): a per-shot clip or a stitched scene (§9.6). */
export interface PlaybackSource {
  kind: SourceKind;
  /** `shot_id` for a shot clip, `scene_id` for a stitched scene. */
  id: string;
  url: string;
}

/** Reference-equality for two playback sources (used to dedup emits). */
function playbackSourceEquals(a: PlaybackSource | null, b: PlaybackSource | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.kind === b.kind && a.id === b.id && a.url === b.url;
}

/** Delete every entry whose key is not in `keep` — prune stale per-book assets. */
function pruneMap<K, V>(map: Map<K, V>, keep: ReadonlySet<K>): void {
  for (const key of [...map.keys()]) if (!keep.has(key)) map.delete(key);
}

/**
 * The active rung of the §12.4 degradation ladder for the beat under the
 * playhead — the best representation currently available, top to bottom:
 *
 * - `full_video` — the committed Wan clip is cached and playable.
 * - `keyframe_ken_burns` — only the beat's keyframe still exists (speculative
 *   zone, §4.4); the shell pans it client-side at zero generation cost.
 * - `illustration` — no keyframe yet, but the book's own page image exists; pan
 *   that instead.
 * - `audio_text_only` — no still at all; the read-along (karaoke text + any
 *   narration) carries the moment. The floor never hard-stops.
 *
 * The rung is a pure function of which assets the engine holds for the beat, so
 * it steps down as the reader outruns rendering and back up as clips arrive —
 * with no path from output back to input, so there is no ownership feedback loop.
 */
export type BeatStage =
  | "full_video"
  | "keyframe_ken_burns"
  | "illustration"
  | "audio_text_only";

export const DEFAULT_GRACE_MS = 1200;
export const DEFAULT_INTENT_DEBOUNCE_MS = 200;

export interface SyncSnapshot {
  /** Who currently drives the playhead — the reader (scroll) or playback (video). */
  owner: ControlOwner;
  mode: SessionMode;
  /** Reading position: the focused source word index. */
  focusWord: number;
  /** Smoothed reading velocity, words/sec. */
  velocity: number;
  currentShotId: string | null;
  /** The beat the current shot dramatizes — keys its keyframe + Ken-Burns motion. */
  currentBeatId: string | null;
  currentClipUrl: string | null;
  /** The current beat's keyframe still, if generated — the Ken-Burns bridge. */
  currentKeyframeUrl: string | null;
  /** The book's own page image for the current beat, if known — the deep fallback. */
  currentIllustrationUrl: string | null;
  /** The active §12.4 ladder rung for what's on the cinema stage right now. */
  currentStage: BeatStage;
  currentPage: number;
  /** Global `word_index` to paint as the karaoke highlight, or null. */
  highlightWordIndex: number | null;
  isPlaying: boolean;
  /** Last reported video-seconds left (budget_low, §5.6); null until reported. */
  budgetRemaining: number | null;
  /**
   * Whether budget pressure has stepped the ladder down (§12.4). Set on
   * `budget_low`, cleared when the committed buffer refills back over the low
   * watermark (`buffer_state`, §4.5). Drives the quiet on-stage notice — never
   * the rung itself (asset-driven).
   */
  underBudgetPressure: boolean;
  /**
   * Committed video-seconds buffered ahead of the playhead (`buffer_state`,
   * §4.5/§4.9) — the live measure of how far the film is rendered ahead of the
   * reader; null until the Scheduler first reports. Crossing back over the low
   * watermark is what steps the ladder back up.
   */
  committedSecondsAhead: number | null;
  /**
   * The playable video source on the cinema stage right now (§9.6): a stitched
   * scene when one covers the playhead (preferred), else the shot's committed
   * clip, else null on the non-video ladder rungs. `currentClipUrl` mirrors its
   * `url`; `kind` tells the shell whether `reportVideoTime` is absolute (scene)
   * or clip-local (shot).
   */
  currentSource: PlaybackSource | null;
  /**
   * The next playable source to preload into a hidden buffer so the boundary
   * swap is gapless (§5.2 "preload the next clip into a hidden buffer element,
   * switch on a clean frame boundary"). Null when nothing renderable is next.
   */
  nextSource: PlaybackSource | null;
  /**
   * Absolute time (within `currentSource`) the player should jump to — set on a
   * deliberate seek (§4.8) or a hot-swap into a freshly stitched scene. The shell
   * applies it whenever `playheadSeekSeq` increments, then resumes playback.
   */
  playheadSeekS: number | null;
  playheadSeekSeq: number;
}

export interface SyncIntent {
  focusWord: number;
  velocity: number;
  mode: SessionMode;
}

export interface SyncEngineCallbacks {
  /** Debounced reading-intent updates for the Scheduler (§4.6). */
  onIntent?: (intent: SyncIntent) => void;
  /** A deliberate jump (scrub/click) — sent to the Scheduler immediately. */
  onSeek?: (word: number) => void;
  /**
   * A playback source's URL failed to load (e.g. an expired presigned URL past
   * its TTL, or a network error) and was dropped — the shell should refetch a
   * fresh URL (e.g. invalidate the shots query) so the engine can recover above
   * the degraded bridge it just fell back to. `id` is the scene_id or shot_id.
   */
  onSourceError?: (id: string) => void;
}

export interface SyncEngineOptions {
  graceMs?: number;
  intentDebounceMs?: number;
  velocity?: VelocityOptions;
  mode?: SessionMode;
  callbacks?: SyncEngineCallbacks;
}

type Listener = () => void;

export class SyncEngine {
  private timeline: TimelineShot[] = [];
  /** The shot under the playhead — the one the ladder represents. */
  private currentShot: TimelineShot | undefined;
  // Bounded LRU caches (§4.8): a backward seek re-reads near-playhead assets,
  // keeping them hot, while far-away ones are shed so a long book never leaks.
  private readonly segments = new LruMap<string, SyncSegment>(192);
  private readonly clipUrls = new LruMap<string, string>(128);
  /** Stitched scenes by `scene_id` — preferred over per-shot clips for their word range (§9.6). */
  private readonly scenes = new LruMap<string, StitchedScene>(48);
  /** Speculative keyframe stills, keyed by beat (§4.4 — one still per beat). */
  private readonly keyframeUrls = new LruMap<string, string>(192);
  /** The book's own page images, keyed by page number — the illustration rung. */
  private readonly illustrationUrls = new LruMap<number, string>(96);
  private readonly velocityTracker: VelocityTracker;
  private readonly graceMs: number;
  private readonly intentDebounceMs: number;
  private readonly callbacks: SyncEngineCallbacks;

  private intentTimer: ReturnType<typeof setTimeout> | null = null;
  private ownerUntilMs = 0;
  private seekSeq = 0;
  private snapshot: SyncSnapshot;
  private readonly listeners = new Set<Listener>();

  constructor(opts: SyncEngineOptions = {}) {
    this.velocityTracker = new VelocityTracker(opts.velocity);
    this.graceMs = opts.graceMs ?? DEFAULT_GRACE_MS;
    this.intentDebounceMs = opts.intentDebounceMs ?? DEFAULT_INTENT_DEBOUNCE_MS;
    this.callbacks = opts.callbacks ?? {};
    this.snapshot = {
      owner: "scroll",
      mode: opts.mode ?? "viewer",
      focusWord: 0,
      velocity: 0,
      currentShotId: null,
      currentBeatId: null,
      currentClipUrl: null,
      currentKeyframeUrl: null,
      currentIllustrationUrl: null,
      currentStage: "audio_text_only",
      currentPage: 0,
      highlightWordIndex: null,
      isPlaying: false,
      budgetRemaining: null,
      underBudgetPressure: false,
      committedSecondsAhead: null,
      currentSource: null,
      nextSource: null,
      playheadSeekS: null,
      playheadSeekSeq: 0,
    };
  }

  // --- useSyncExternalStore interface ------------------------------------- #

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): SyncSnapshot => this.snapshot;

  private emit(patch: Partial<SyncSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) listener();
  }

  // --- data inputs -------------------------------------------------------- #

  /**
   * Load the book's shot list (the timeline the playhead resolves against).
   *
   * Called again as the list grows during ingest — and potentially for a
   * different book on a reused engine — so it **prunes** every cached asset that
   * no longer belongs to the current shot/beat/scene set rather than blowing the
   * caches away. That keeps live scenes/keyframes while a book fills in, but
   * never serves a previous book's stale stitched scene or clip.
   */
  setShots(shots: readonly ShotResponse[]): void {
    this.timeline = buildTimeline(shots);
    const shotIds = new Set(this.timeline.map((s) => s.shotId));
    const beatIds = new Set(
      this.timeline.map((s) => s.beatId).filter((b): b is string => b !== null),
    );
    const sceneIds = new Set(
      this.timeline.map((s) => s.sceneId).filter((s): s is string => s !== null),
    );
    const pages = new Set(this.timeline.map((s) => s.page));
    pruneMap(this.clipUrls, shotIds);
    pruneMap(this.segments, shotIds);
    pruneMap(this.scenes, sceneIds);
    pruneMap(this.keyframeUrls, beatIds);
    pruneMap(this.illustrationUrls, pages);
    if (this.currentShot && !shotIds.has(this.currentShot.shotId)) this.currentShot = undefined;

    for (const shot of this.timeline) {
      if (shot.clipUrl) this.clipUrls.set(shot.shotId, shot.clipUrl);
    }
    this.resolveShotForWord(this.snapshot.focusWord);
  }

  /** Hot-swap: record a freshly rendered clip + its sync segment (clip_ready). */
  ingestClip(segment: SyncSegment, clipUrl?: string | null): void {
    this.segments.set(segment.shot_id, segment);
    if (clipUrl) {
      const prev = this.clipUrls.get(segment.shot_id);
      this.clipUrls.set(segment.shot_id, clipUrl);
      // A re-render of a shot already folded into a stitched scene makes that
      // scene's concatenated mp4 stale — drop it so the playhead falls back to
      // the fresh per-shot clip until the backend re-stitches (§9.6).
      if (prev && prev !== clipUrl) this.evictSceneContaining(segment.shot_id);
      // A committed clip arriving means the buffer is refilling — release the
      // budget pressure that stepped us down the ladder (§12.4 steps back up).
      if (this.snapshot.underBudgetPressure) this.emit({ underBudgetPressure: false });
    }
    // The clip may be for the shot on screen (hot-swap) or the upcoming one
    // (refresh the preload target); syncCurrent recomputes both.
    this.syncCurrent();
  }

  /**
   * Record a stitched scene (`scene_stitched`, §9.6) and prefer it over the
   * per-shot clips for its word range. If the playhead is already inside the
   * scene, hot-swap from per-shot playback to the one continuous asset and seek
   * it to the current word so the film does not jump; otherwise the scene simply
   * becomes the preload target for the upcoming boundary. The transition is
   * "seamless" because the stitched scene is a single source — within it there
   * are no clip swaps at all.
   */
  ingestScene(
    sceneId: string,
    clipUrl: string | null | undefined,
    segments: readonly SyncSegment[],
  ): void {
    const scene = buildStitchedScene(sceneId, clipUrl, segments, this.timeline);
    if (!scene) return;
    const prev = this.scenes.get(sceneId);
    this.scenes.set(sceneId, scene);

    const word = this.snapshot.focusWord;
    const cur = this.snapshot.currentSource;
    const switchingFromShot = cur?.kind === "shot";
    // A re-stitch (a new mp4 for a scene we're already on, e.g. after a regen) —
    // supersede the stale one in place rather than restarting at 0.
    const reStitched = cur?.kind === "scene" && cur.id === sceneId && prev?.clipUrl !== scene.clipUrl;
    const covers = word >= scene.startWord && word <= scene.endWord;

    // Recompute the stage — now scene-aware, so it may adopt the stitched asset.
    this.syncCurrent();

    const onThisScene =
      this.snapshot.currentSource?.kind === "scene" && this.snapshot.currentSource.id === sceneId;
    if (covers && onThisScene && (switchingFromShot || reStitched)) {
      this.requestSeek(sceneTimeForWord(scene, word));
    }
  }

  /** The clip URL currently known for a shot (its keyframe/clip), or null. */
  getClipUrl(shotId: string): string | null {
    return this.clipUrls.get(shotId) ?? null;
  }

  /**
   * Swap only a shot's clip URL, keeping its existing sync segment (`regen_done`,
   * §5.6). A Director edit changes pixels, not narration timing, so the word
   * timestamps still hold — we just point the source at the new take. If it is the
   * shot on screen, the stage hot-swaps in place via the recomputed snapshot.
   */
  swapClipUrl(shotId: string, clipUrl?: string | null): void {
    if (!clipUrl) return;
    this.clipUrls.set(shotId, clipUrl);
    // A regenerated shot makes any stitched scene that contains it stale (§9.6) —
    // drop it so the fresh take shows instead of the old concatenated mp4.
    const evicted = this.evictSceneContaining(shotId);
    if (evicted || shotId === this.currentShot?.shotId) this.syncCurrent();
  }

  /**
   * Drop a failed playback source so the playhead re-resolves to the next-best
   * rung instead of freezing on a dead asset (an expired presigned URL past its
   * TTL, or a network error). A scene falls back to its per-shot clips; a shot
   * falls back to the §12.4 keyframe/Ken-Burns bridge. Fires `onSourceError` so
   * the shell can refetch a fresh URL and recover above the bridge.
   */
  markSourceFailed(sourceId: string): void {
    let dropped = false;
    if (this.scenes.delete(sourceId)) dropped = true;
    if (this.clipUrls.delete(sourceId)) dropped = true;
    if (!dropped) return;
    this.syncCurrent();
    this.callbacks.onSourceError?.(sourceId);
  }

  /** Drop any stitched scene that contains `shotId`; returns whether one was removed. */
  private evictSceneContaining(shotId: string): boolean {
    let evicted = false;
    for (const [id, scene] of this.scenes) {
      if (scene.segments.some((seg) => seg.shot_id === shotId)) {
        this.scenes.delete(id);
        evicted = true;
      }
    }
    return evicted;
  }

  /**
   * Cache a beat's keyframe still (keyframe_ready, §5.6) so the speculative zone
   * Ken-Burns'es over it instead of stalling. Keyed by beat, so a backward seek
   * to a beat already seen is an instant cache hit. Recomputes the stage only if
   * it is the beat on screen.
   */
  ingestKeyframe(beatId: string, oss_url?: string | null): void {
    if (!beatId || !oss_url || this.keyframeUrls.get(beatId) === oss_url) return;
    this.keyframeUrls.set(beatId, oss_url);
    if (beatId === this.currentShot?.beatId) this.syncCurrent();
  }

  /**
   * Register the book's own image for a page (from Phase-A page extraction) — the
   * illustration rung the shell pans when no keyframe exists yet. The shell feeds
   * it as it loads each page; keyed by page number.
   */
  setPageIllustration(page: number, imageUrl?: string | null): void {
    if (page == null || !imageUrl || this.illustrationUrls.get(page) === imageUrl) return;
    this.illustrationUrls.set(page, imageUrl);
    if (this.currentShot?.page === page) this.syncCurrent();
  }

  /**
   * The still URLs (a beat's keyframe, else its page illustration) for the current
   * beat and the next `count` shots — the shell preloads/decodes these so the §4.4
   * Ken-Burns bridge paints instantly when the reader arrives, with no image-load
   * flash. Read-only: it `peek`s, so it does not disturb cache recency.
   */
  upcomingStillUrls(count = 3): string[] {
    const cur = this.currentShot;
    const start = cur
      ? this.timeline.findIndex((s) => s.shotId === cur.shotId)
      : shotIndexForWord(this.timeline, this.snapshot.focusWord);
    if (start < 0) return [];
    const urls: string[] = [];
    for (let i = start; i <= start + count && i < this.timeline.length; i++) {
      const shot = this.timeline[i];
      if (!shot) continue;
      const still =
        (shot.beatId ? this.keyframeUrls.peek(shot.beatId) : undefined) ??
        this.illustrationUrls.peek(shot.page);
      if (still && !urls.includes(still)) urls.push(still);
    }
    return urls;
  }

  /**
   * Drop a beat's keyframe still after its URL failed to load (e.g. an expired
   * presigned URL past its TTL) so the ladder falls through to the illustration /
   * audio-text floor instead of showing a broken image; if it is the beat on
   * screen, the stage re-resolves immediately. The shell calls this from the
   * still's `onError` and may then refetch a fresh URL.
   */
  dropKeyframe(beatId: string): void {
    if (!beatId || !this.keyframeUrls.has(beatId)) return;
    this.keyframeUrls.delete(beatId);
    if (beatId === this.currentShot?.beatId) this.syncCurrent();
  }

  /** Drop a page's illustration after its URL failed to load (see {@link dropKeyframe}). */
  dropIllustration(page: number): void {
    if (page == null || !this.illustrationUrls.has(page)) return;
    this.illustrationUrls.delete(page);
    if (this.currentShot?.page === page) this.syncCurrent();
  }

  /**
   * Drop whichever still is on the bridge right now after its image failed to
   * load — the keyframe if that's the active rung, else the page illustration —
   * so the ladder walks down one rung (§12.4). The shell calls this from the
   * degraded still's `onError`; repeated failures walk all the way to the floor.
   */
  dropCurrentStill(): void {
    const shot = this.currentShot;
    if (!shot) return;
    if (shot.beatId && this.keyframeUrls.has(shot.beatId)) this.dropKeyframe(shot.beatId);
    else this.dropIllustration(shot.page);
  }

  /**
   * Note the Scheduler's budget_low (§5.6): record the remaining video-seconds
   * and put the ladder into its degraded posture. Does not itself change the rung
   * (that is asset-driven) — it drives the quiet on-stage notice, and is released
   * when a clip shows the buffer refilling (see {@link ingestClip}).
   */
  noteBudgetLow(remainingS: number): void {
    if (this.snapshot.underBudgetPressure && this.snapshot.budgetRemaining === remainingS) {
      return;
    }
    this.emit({ underBudgetPressure: true, budgetRemaining: remainingS });
  }

  /**
   * Note the Scheduler's `buffer_state` tick (§4.5/§4.9): track committed
   * video-seconds buffered ahead, and when that refills back over the low
   * watermark, release the budget pressure — the §12.4 "silently steps back up",
   * using the live buffer signal rather than waiting on the next clip alone.
   * (Deliberately does not touch `budgetRemaining`: a non-null value there means
   * "low", which only `budget_low` should assert.)
   */
  noteBufferState(state: { committedSecondsAhead: number; lowWatermarkS: number }): void {
    const patch: Partial<SyncSnapshot> = {};
    if (state.committedSecondsAhead !== this.snapshot.committedSecondsAhead) {
      patch.committedSecondsAhead = state.committedSecondsAhead;
    }
    if (this.snapshot.underBudgetPressure && state.committedSecondsAhead >= state.lowWatermarkS) {
      patch.underBudgetPressure = false;
    }
    if (Object.keys(patch).length > 0) this.emit(patch);
  }

  setMode(mode: SessionMode): void {
    if (mode !== this.snapshot.mode) {
      this.emit({ mode });
      this.scheduleIntent();
    }
  }

  setPlaying(isPlaying: boolean): void {
    if (isPlaying !== this.snapshot.isPlaying) this.emit({ isPlaying });
  }

  // --- control inputs ----------------------------------------------------- #

  /** The reader scrolled to `focusWord` — continuous reading drives intent. */
  reportScroll(focusWord: number, nowMs: number): void {
    this.takeOwnership("scroll", nowMs);
    const velocity = this.velocityTracker.sample(focusWord, nowMs);
    this.emit({ focusWord, velocity });
    this.resolveShotForWord(focusWord);
    this.scheduleIntent();
  }

  /**
   * A deliberate jump to `word` (scrub/click/search) — re-seeds the playhead
   * (§4.8): resets velocity, resolves the source, and jumps the active asset to
   * the word's frame. When the word is inside a stitched scene this lands
   * mid-scene on the correct frame + karaoke highlight, rather than restarting
   * the asset; the next `reportVideoTime` then carries the read-along from there.
   */
  seek(word: number, nowMs: number): void {
    this.takeOwnership("scroll", nowMs);
    this.velocityTracker.reset();
    this.emit({ focusWord: word, velocity: 0 });
    this.resolveShotForWord(word);
    const source = this.snapshot.currentSource;
    if (source) this.requestSeek(this.sourceTimeForWord(source, word));
    this.callbacks.onSeek?.(word);
  }

  /**
   * The active asset played to its end — flow continuously past the boundary
   * (viewer mode, §5.3). Prefers the preloaded `nextSource` (a gapless swap); if
   * no committed clip is queued, still advances onto the next beat so the §12.4
   * keyframe/Ken-Burns bridge fills the gap rather than a dead stop ("below the
   * floor, the keyframe/Ken-Burns ladder fills the gap with no stall", §4.11).
   * Returns whether the playhead advanced at all (`false` only at end of book).
   *
   * Playback-driven, so it does not grab scroll ownership: the next source starts
   * at its head (time 0) and its first `reportVideoTime` carries the read-along on.
   */
  advanceToNextSource(): boolean {
    const next = this.snapshot.nextSource;
    if (next) {
      const startWord = this.sourceStartWord(next);
      if (startWord !== null) {
        this.emit({ focusWord: startWord });
        this.resolveShotForWord(startWord);
        if (this.snapshot.currentSource?.id === next.id) return true;
      }
    }
    return this.advanceToNextBeat();
  }

  /** Advance onto the next beat regardless of whether its clip is ready (bridge fallback, §4.11). */
  private advanceToNextBeat(): boolean {
    const current = this.currentShot;
    if (!current) return false;
    const idx = this.timeline.findIndex((s) => s.shotId === current.shotId);
    const nextShot = idx >= 0 ? this.timeline[idx + 1] : undefined;
    if (!nextShot) return false;
    this.emit({ focusWord: nextShot.startWord });
    this.resolveShotForWord(nextShot.startWord);
    return true;
  }

  /**
   * Playback advanced to `tSec` within the on-screen asset — drives read-along.
   * For a stitched scene `tSec` is absolute scene time; for a shot clip it is
   * clip-local time. `sourceId` is the asset the shell is actually playing: if it
   * differs from `currentSource` (the shell crossed a boundary into `nextSource`)
   * the engine adopts it before interpreting the time, so the highlight never
   * snaps back to the previous clip during a gapless swap.
   */
  reportVideoTime(tSec: number, nowMs: number, sourceId?: string): void {
    // Ignore playback updates during the grace window right after the reader scrolled.
    if (this.snapshot.owner === "scroll" && nowMs < this.ownerUntilMs) return;
    this.takeOwnership("video", nowMs);

    let source = this.snapshot.currentSource;
    if (sourceId && source?.id !== sourceId) {
      const adopted = this.sourceById(sourceId);
      if (adopted) {
        source = adopted;
        this.emit({
          currentSource: adopted,
          currentClipUrl: adopted.url,
          currentStage: "full_video",
          nextSource: this.computeNextSource(adopted),
        });
      }
    }
    if (!source) return;

    if (source.kind === "scene") {
      const scene = this.scenes.get(source.id);
      if (!scene) return;
      const idx = segmentIndexAtTime(scene, tSec);
      const segment = idx >= 0 ? scene.segments[idx] : undefined;
      if (!segment) return;
      const highlight = highlightedWordIndexAt(segment, tSec);
      const page = shouldTurnPage(segment, tSec) ? this.sceneSegmentPage(scene, idx) : segment.page;
      const patch: Partial<SyncSnapshot> = {
        highlightWordIndex: highlight,
        currentPage: page,
        currentShotId: segment.shot_id,
      };
      if (highlight !== null) patch.focusWord = highlight;
      this.emitIfChanged(patch);
      return;
    }

    const segment = this.segments.get(source.id);
    if (!segment) return;
    const highlight = highlightedWordIndexAt(segment, tSec);
    const page = shouldTurnPage(segment, tSec) ? this.pageAfter(segment) : segment.page;
    const patch: Partial<SyncSnapshot> = { highlightWordIndex: highlight, currentPage: page };
    if (highlight !== null) patch.focusWord = highlight;
    this.emitIfChanged(patch);
  }

  dispose(): void {
    if (this.intentTimer) clearTimeout(this.intentTimer);
    this.intentTimer = null;
    this.listeners.clear();
  }

  // --- internals ---------------------------------------------------------- #

  private takeOwnership(who: ControlOwner, nowMs: number): void {
    if (this.snapshot.owner !== who) this.emit({ owner: who });
    this.ownerUntilMs = nowMs + this.graceMs;
  }

  private resolveShotForWord(word: number): void {
    const index = shotIndexForWord(this.timeline, word);
    const shot = index >= 0 ? this.timeline[index] : undefined;
    const shotChanged = (shot?.shotId ?? null) !== this.snapshot.currentShotId;
    this.currentShot = shot;
    // The page follows the playhead only when the shot itself changes (a scroll /
    // seek); within a shot, page-turns are driven by playback (reportVideoTime).
    this.syncCurrent({ updatePage: shotChanged });
  }

  /**
   * Recompute the on-stage representation for `currentShot` from the assets held,
   * emitting only the fields that actually changed — so an asset arriving for an
   * off-screen shot is a silent cache write, and one for the on-screen shot
   * hot-swaps the stage. The rung is chosen top-down: clip → keyframe →
   * illustration → audio/text floor (§12.4).
   */
  private syncCurrent(opts: { updatePage?: boolean } = {}): void {
    const shot = this.currentShot;
    const shotId = shot?.shotId ?? null;
    const beatId = shot?.beatId ?? null;

    // A stitched scene (one continuous asset) outranks the per-shot clip for its
    // word range (§9.6) — within it there are no clip swaps, so playback is
    // gapless. The stage plays the scene mp4 when one covers the playhead.
    const scene = this.stitchedSceneForWord(this.snapshot.focusWord);
    const shotClipUrl = shot ? this.clipUrls.get(shot.shotId) ?? null : null;
    const source: PlaybackSource | null = scene
      ? { kind: "scene", id: scene.sceneId, url: scene.clipUrl }
      : shotClipUrl && shotId
        ? { kind: "shot", id: shotId, url: shotClipUrl }
        : null;
    const clipUrl = source?.url ?? null;
    const nextSource = this.computeNextSource(source);

    const keyframeUrl = beatId ? this.keyframeUrls.get(beatId) ?? null : null;
    const illustrationUrl = shot ? this.illustrationUrls.get(shot.page) ?? null : null;
    const stage: BeatStage = clipUrl
      ? "full_video"
      : keyframeUrl
        ? "keyframe_ken_burns"
        : illustrationUrl
          ? "illustration"
          : "audio_text_only";

    const patch: Partial<SyncSnapshot> = {};
    if (shotId !== this.snapshot.currentShotId) patch.currentShotId = shotId;
    if (beatId !== this.snapshot.currentBeatId) patch.currentBeatId = beatId;
    if (clipUrl !== this.snapshot.currentClipUrl) patch.currentClipUrl = clipUrl;
    if (keyframeUrl !== this.snapshot.currentKeyframeUrl) patch.currentKeyframeUrl = keyframeUrl;
    if (illustrationUrl !== this.snapshot.currentIllustrationUrl) {
      patch.currentIllustrationUrl = illustrationUrl;
    }
    if (stage !== this.snapshot.currentStage) patch.currentStage = stage;
    if (!playbackSourceEquals(source, this.snapshot.currentSource)) patch.currentSource = source;
    if (!playbackSourceEquals(nextSource, this.snapshot.nextSource)) patch.nextSource = nextSource;
    if (opts.updatePage && shot && shot.page !== this.snapshot.currentPage) {
      patch.currentPage = shot.page;
    }
    if (Object.keys(patch).length > 0) this.emit(patch);
  }

  private pageAfter(segment: SyncSegment): number {
    return this.pageAfterShot(segment.shot_id, segment.page);
  }

  /** The next shot's page (if it advances) for the page-turn lead, else `page`. */
  private pageAfterShot(shotId: string, page: number): number {
    const index = this.timeline.findIndex((s) => s.shotId === shotId);
    const next = index >= 0 ? this.timeline[index + 1] : undefined;
    return next && next.page > page ? next.page : page;
  }

  /** Emit only if at least one field in `patch` actually differs (avoids 60fps re-render churn). */
  private emitIfChanged(patch: Partial<SyncSnapshot>): void {
    const keys = Object.keys(patch) as (keyof SyncSnapshot)[];
    if (keys.some((k) => this.snapshot[k] !== patch[k])) this.emit(patch);
  }

  /** Ask the shell to jump the active asset to `toS` (a deliberate seek / hot-swap). */
  private requestSeek(toS: number): void {
    this.emit({ playheadSeekS: toS, playheadSeekSeq: ++this.seekSeq });
  }

  /** A stitched scene whose word range covers `word`, or null. */
  private stitchedSceneForWord(word: number): StitchedScene | null {
    for (const scene of this.scenes.values()) {
      if (word >= scene.startWord && word <= scene.endWord) return scene;
    }
    return null;
  }

  /** The source covering the word right after `current` ends — for hidden preloading (§5.2). */
  private computeNextSource(current: PlaybackSource | null): PlaybackSource | null {
    if (!current) return null;
    let nextWord: number;
    if (current.kind === "scene") {
      const scene = this.scenes.get(current.id);
      if (!scene) return null;
      nextWord = scene.endWord + 1;
    } else {
      const idx = this.timeline.findIndex((s) => s.shotId === current.id);
      const shot = idx >= 0 ? this.timeline[idx] : undefined;
      if (!shot) return null;
      nextWord = shot.endWord + 1;
    }

    const scene = this.stitchedSceneForWord(nextWord);
    if (scene && scene.sceneId !== current.id) {
      return { kind: "scene", id: scene.sceneId, url: scene.clipUrl };
    }
    if (!scene) {
      const idx = shotIndexForWord(this.timeline, nextWord);
      const shot = idx >= 0 ? this.timeline[idx] : undefined;
      // The shot must actually contain the boundary word — `shotIndexForWord`
      // returns the last shot at/before `nextWord`, which past the end would be
      // the current shot's own scene tail (nothing genuinely next).
      if (shot && shot.shotId !== current.id && nextWord >= shot.startWord && nextWord <= shot.endWord) {
        const url = this.clipUrls.get(shot.shotId);
        if (url) return { kind: "shot", id: shot.shotId, url };
      }
    }
    return null;
  }

  /** The page to show once segment `idx` turns: the next scene segment's page, else the next shot's. */
  private sceneSegmentPage(scene: StitchedScene, idx: number): number {
    const segment = scene.segments[idx];
    if (!segment) return this.snapshot.currentPage;
    const next = scene.segments[idx + 1];
    if (next && next.page > segment.page) return next.page;
    return this.pageAfterShot(segment.shot_id, segment.page);
  }

  /** The time within `source` at which `word` is narrated (absolute for a scene, clip-local for a shot). */
  private sourceTimeForWord(source: PlaybackSource, word: number): number {
    if (source.kind === "scene") {
      const scene = this.scenes.get(source.id);
      return scene ? sceneTimeForWord(scene, word) : 0;
    }
    const segment = this.segments.get(source.id);
    if (!segment) return 0;
    let tSec = 0;
    for (const w of segment.words) {
      if (w.word_index <= word) tSec = w.t_start;
      else break;
    }
    return tSec;
  }

  /** Resolve a source by id (scene first, then a shot clip) — used to adopt what the shell plays. */
  private sourceById(id: string): PlaybackSource | null {
    const scene = this.scenes.get(id);
    if (scene) return { kind: "scene", id, url: scene.clipUrl };
    const url = this.clipUrls.get(id);
    if (url && this.timeline.some((s) => s.shotId === id)) {
      return { kind: "shot", id, url };
    }
    return null;
  }

  /** The first source word of a playback source — where a boundary auto-advance enters it. */
  private sourceStartWord(source: PlaybackSource): number | null {
    if (source.kind === "scene") return this.scenes.get(source.id)?.startWord ?? null;
    const shot = this.timeline.find((s) => s.shotId === source.id);
    return shot ? shot.startWord : null;
  }

  private scheduleIntent(): void {
    if (!this.callbacks.onIntent) return;
    if (this.intentTimer) clearTimeout(this.intentTimer);
    this.intentTimer = setTimeout(() => {
      this.intentTimer = null;
      this.callbacks.onIntent?.({
        focusWord: this.snapshot.focusWord,
        velocity: this.snapshot.velocity,
        mode: this.snapshot.mode,
      });
    }, this.intentDebounceMs);
  }
}
