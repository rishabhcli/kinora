import type {
  IntentUpdate,
  SessionMode,
  Shot,
  SyncMap,
  SyncSegment,
} from "../api/types";
import {
  activeWordIndexAt,
  localTime,
  seekTargetForWord,
  shotForWord,
  shotIndexForWord,
  shouldTurnPage,
} from "../lib/syncmap";
import { VelocityTracker, type VelocityOptions } from "../lib/velocity";

// The SyncEngine is the client-side single source of truth for the playhead
// (kinora.md §5.2). It supports BIDIRECTIONAL linkage between the page and the
// video without a feedback loop, computes the reading position (`w`) and
// velocity (`v`), pushes debounced intent to the Scheduler, hot-swaps clips as
// they arrive, and bridges seeks with a client-side Ken-Burns pan.
//
// It is deliberately framework-agnostic: state is exposed via a
// subscribe/getSnapshot pair (consumed by React's useSyncExternalStore) and
// every time-sensitive entry point accepts an explicit `nowMs`, so the
// owner-token grace, EWMA clamp and debounce are all unit-testable.

export const GRACE_MS = 1200;
export const INTENT_DEBOUNCE_MS = 200;

export type ControlOwner = "scroll" | "video";

export interface SyncSnapshot {
  owner: ControlOwner;
  mode: SessionMode;
  focusWord: number;
  velocity: number;
  currentShotId: string | null;
  currentPage: number | null;
  activeWordIndex: number | null;
  /** URL playing in the visible <video>. */
  videoSrc: string | null;
  /** URL warming in the hidden buffer element, ready for a seamless swap. */
  preloadSrc: string | null;
  /** Keyframe shown under a Ken-Burns pan while real video renders. */
  bridgeKeyframeUrl: string | null;
  bridging: boolean;
  committedSecondsAhead: number;
  /** Bumped whenever the player should seek; paired with `seekToS`. */
  seekNonce: number;
  seekToS: number;
  budgetRemaining: number | null;
}

export interface SyncEngineConfig {
  sessionId: string;
  /** Debounced intent push to POST /sessions/:id/intent. */
  pushIntent: (intent: IntentUpdate) => void;
  /** Explicit jump → POST /sessions/:id/seek. */
  postSeek?: (word: number) => void;
  now?: () => number;
  graceMs?: number;
  debounceMs?: number;
  velocity?: VelocityOptions;
}

const DEFAULT_SHOT_DURATION_S = 5;

function shotDuration(shot: Shot): number {
  return shot.duration_s ?? shot.est_duration_s ?? DEFAULT_SHOT_DURATION_S;
}

export class SyncEngine {
  private readonly sessionId: string;
  private readonly pushIntentFn: (intent: IntentUpdate) => void;
  private readonly postSeekFn?: (word: number) => void;
  private readonly now: () => number;
  private readonly graceMs: number;
  private readonly debounceMs: number;
  private readonly velocity: VelocityTracker;

  private listeners = new Set<() => void>();
  private snapshot: SyncSnapshot;

  private shots: Shot[] = [];
  private shotIndexById = new Map<string, number>();
  private segments = new Map<string, SyncSegment>();
  private clips = new Map<string, string>();
  private keyframesByShot = new Map<string, string>();
  private keyframesByBeat = new Map<string, string>();
  private beatByShot = new Map<string, string>();

  private owner: ControlOwner = "video";
  private graceUntilMs = 0;
  private mode: SessionMode = "viewer";
  private focusWord = 0;
  private velocityValue: number;
  private currentShotId: string | null = null;
  private currentLocalTimeS = 0;
  private budgetRemaining: number | null = null;

  private intentTimer: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;

  constructor(config: SyncEngineConfig) {
    this.sessionId = config.sessionId;
    this.pushIntentFn = config.pushIntent;
    this.postSeekFn = config.postSeek;
    this.now = config.now ?? (() => Date.now());
    this.graceMs = config.graceMs ?? GRACE_MS;
    this.debounceMs = config.debounceMs ?? INTENT_DEBOUNCE_MS;
    this.velocity = new VelocityTracker(config.velocity);
    this.velocityValue = this.velocity.value;
    this.snapshot = {
      owner: "video",
      mode: "viewer",
      focusWord: 0,
      velocity: this.velocityValue,
      currentShotId: null,
      currentPage: null,
      activeWordIndex: null,
      videoSrc: null,
      preloadSrc: null,
      bridgeKeyframeUrl: null,
      bridging: false,
      committedSecondsAhead: 0,
      seekNonce: 0,
      seekToS: 0,
      budgetRemaining: null,
    };
  }

  // -- React store interface ------------------------------------------------
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): SyncSnapshot => this.snapshot;

  // -- Configuration --------------------------------------------------------
  setShots(shots: Shot[]): void {
    this.shots = [...shots].sort(
      (a, b) => a.source_span.word_range[0] - b.source_span.word_range[0],
    );
    this.shotIndexById.clear();
    this.beatByShot.clear();
    this.shots.forEach((shot, i) => {
      this.shotIndexById.set(shot.shot_id, i);
      this.beatByShot.set(shot.shot_id, shot.beat_id);
      if (shot.clip_url) this.clips.set(shot.shot_id, shot.clip_url);
      if (shot.keyframe_url) this.keyframesByShot.set(shot.shot_id, shot.keyframe_url);
    });
    this.recomputeBuffer();
    this.emit();
  }

  setMode(mode: SessionMode): void {
    if (this.mode === mode) return;
    this.mode = mode;
    this.emit();
  }

  // -- Reading-position input (scroll) -------------------------------------
  /** The reader scrolled the PDF: grab ownership, update w & v, seek video. */
  onScrollInput(focusWord: number, nowMs: number = this.now()): void {
    this.grabScrollOwnership(nowMs);
    this.focusWord = focusWord;
    this.velocityValue = this.velocity.sample(focusWord, nowMs);

    const shot = shotForWord(this.shots, focusWord);
    if (shot) {
      this.currentShotId = shot.shot_id;
      this.driveVideoFromScroll(shot, focusWord);
    }
    this.scheduleIntent();
    this.recomputeBuffer();
    this.emit();
  }

  /** Explicit jump (tap a far page, scrub the timeline, search) — §4.8. */
  seek(word: number, nowMs: number = this.now()): void {
    this.grabScrollOwnership(nowMs);
    this.velocity.reset();
    this.focusWord = word;
    this.velocityValue = this.velocity.value;

    const shot = shotForWord(this.shots, word);
    if (shot) {
      this.currentShotId = shot.shot_id;
      this.snapshot = { ...this.snapshot, currentPage: shot.source_span.page };
      // 2. Bridge instantly with the keyframe under a Ken-Burns pan.
      this.showBridge(shot);
      // If the clip already exists (e.g. a backward seek → cache hit), play it.
      const url = this.clips.get(shot.shot_id);
      if (url) {
        const seg = this.segments.get(shot.shot_id);
        const target = seg ? seekTargetForWord({ scene_id: "", segments: [seg] }, word) : null;
        this.setVideoSrc(url);
        this.requestSeek(target ? target.videoTimeS : 0);
      } else {
        this.setVideoSrc(null);
      }
    }
    // 3. Re-seed: tell the backend (velocity already reset to default).
    this.postSeekFn?.(word);
    this.scheduleIntent();
    this.recomputeBuffer();
    this.emit();
  }

  // -- Video playhead input -------------------------------------------------
  /** The visible video advanced; in Viewer mode it drives the page. */
  onVideoTime(videoTimeS: number, nowMs: number = this.now()): void {
    this.currentLocalTimeS = videoTimeS;

    // Real video is rendering → drop the Ken-Burns bridge, regardless of who
    // owns the playhead (the keyframe is literally covered by live frames).
    let bridging = this.snapshot.bridging;
    let bridgeKeyframeUrl = this.snapshot.bridgeKeyframeUrl;
    if (bridging && this.snapshot.videoSrc && videoTimeS > 0) {
      bridging = false;
      bridgeKeyframeUrl = null;
    }

    // Owner arbitration (kinora.md §5.2): manual scroll owns the playhead for a
    // grace window and suppresses the video-driven page-turn; once the reader
    // stops touching the PDF, a video tick past the grace reclaims ownership.
    if (this.owner === "scroll") {
      if (nowMs < this.graceUntilMs) {
        this.snapshot = { ...this.snapshot, bridging, bridgeKeyframeUrl };
        this.recomputeBuffer();
        this.emit();
        return; // suppressed — no page-turn during grace
      }
      this.owner = "video";
    }

    const seg = this.currentSegment();
    let activeWord: number | null = this.snapshot.activeWordIndex;
    let page: number | null = this.snapshot.currentPage;
    if (seg) {
      const lt = localTime(seg, videoTimeS);
      activeWord = activeWordIndexAt(seg.words, lt);
      page = shouldTurnPage(seg, videoTimeS) ? this.nextPage(seg) : seg.page;
    }
    this.snapshot = {
      ...this.snapshot,
      owner: this.owner,
      activeWordIndex: activeWord,
      currentPage: page,
      bridging,
      bridgeKeyframeUrl,
    };
    this.recomputeBuffer();
    this.emit();
  }

  /** The current clip ended — advance to the next shot on a clean boundary. */
  onVideoEnded(): void {
    const idx = this.currentShotId ? this.shotIndexById.get(this.currentShotId) : undefined;
    if (idx === undefined) return;
    const next = this.shots[idx + 1];
    if (!next) {
      this.setVideoSrc(null);
      this.emit();
      return;
    }
    this.currentShotId = next.shot_id;
    this.currentLocalTimeS = 0;
    const url = this.clips.get(next.shot_id);
    if (url) {
      // The hidden buffer already warmed this URL → swap is seamless.
      this.setVideoSrc(url);
      this.requestSeek(0);
      this.snapshot = { ...this.snapshot, bridging: false, bridgeKeyframeUrl: null };
    } else {
      this.showBridge(next);
      this.setVideoSrc(null);
    }
    this.preloadNext();
    this.recomputeBuffer();
    this.emit();
  }

  // -- Generation events ----------------------------------------------------
  /** clip_ready → cache + hot-swap (kinora.md §5.6). */
  registerClip(shotId: string, url: string, segment?: SyncSegment): void {
    this.clips.set(shotId, url);
    if (segment) this.segments.set(shotId, segment);

    if (this.currentShotId === shotId && (this.snapshot.bridging || !this.snapshot.videoSrc)) {
      // We were bridging this exact shot → the real clip catches up.
      this.setVideoSrc(url);
      this.requestSeek(this.currentLocalTimeS);
    } else if (this.isImmediateNext(shotId)) {
      this.snapshot = { ...this.snapshot, preloadSrc: url }; // warm the buffer
    }
    this.recomputeBuffer();
    this.emit();
  }

  /** keyframe_ready → cache the still for the Ken-Burns bridge. */
  registerKeyframe(beatId: string, url: string, shotId?: string): void {
    this.keyframesByBeat.set(beatId, url);
    if (shotId) this.keyframesByShot.set(shotId, url);
    this.emit();
  }

  /** scene_stitched → replace per-shot playback with the stitched scene. */
  registerScene(map: SyncMap, url: string): void {
    for (const seg of map.segments) {
      this.segments.set(seg.shot_id, seg);
      this.clips.set(seg.shot_id, url);
    }
    this.recomputeBuffer();
    this.emit();
  }

  /** regen_done → swap a single shot after a Director edit. */
  registerRegen(shotId: string, url: string): void {
    this.clips.set(shotId, url);
    if (this.currentShotId === shotId) {
      this.setVideoSrc(url);
      this.requestSeek(this.currentLocalTimeS);
    }
    this.emit();
  }

  setBudgetRemaining(seconds: number): void {
    this.budgetRemaining = seconds;
    this.snapshot = { ...this.snapshot, budgetRemaining: seconds };
    this.emit();
  }

  destroy(): void {
    this.destroyed = true;
    if (this.intentTimer) clearTimeout(this.intentTimer);
    this.intentTimer = null;
    this.listeners.clear();
  }

  // -- Internals ------------------------------------------------------------
  private grabScrollOwnership(nowMs: number): void {
    this.owner = "scroll";
    this.graceUntilMs = nowMs + this.graceMs;
  }

  private driveVideoFromScroll(shot: Shot, focusWord: number): void {
    const seg = this.segments.get(shot.shot_id);
    const url = this.clips.get(shot.shot_id);
    if (seg && url) {
      const target = seekTargetForWord({ scene_id: "", segments: [seg] }, focusWord);
      this.setVideoSrc(url);
      this.requestSeek(target ? target.videoTimeS : seg.video_start_s);
      this.snapshot = { ...this.snapshot, bridging: false, bridgeKeyframeUrl: null };
    } else {
      this.showBridge(shot);
      this.setVideoSrc(null);
    }
  }

  private showBridge(shot: Shot): void {
    const url =
      this.keyframesByShot.get(shot.shot_id) ??
      this.keyframesByBeat.get(shot.beat_id) ??
      shot.keyframe_url ??
      null;
    this.snapshot = { ...this.snapshot, bridging: true, bridgeKeyframeUrl: url };
  }

  private setVideoSrc(url: string | null): void {
    this.snapshot = { ...this.snapshot, videoSrc: url };
  }

  private requestSeek(toS: number): void {
    this.snapshot = {
      ...this.snapshot,
      seekNonce: this.snapshot.seekNonce + 1,
      seekToS: Math.max(0, toS),
    };
  }

  private currentSegment(): SyncSegment | undefined {
    return this.currentShotId ? this.segments.get(this.currentShotId) : undefined;
  }

  private nextPage(seg: SyncSegment): number {
    if (!this.currentShotId) return seg.page;
    const idx = this.shotIndexById.get(this.currentShotId);
    if (idx === undefined) return seg.page + 1;
    const next = this.shots[idx + 1];
    const nextSeg = next ? this.segments.get(next.shot_id) : undefined;
    return nextSeg ? nextSeg.page : seg.page + 1;
  }

  private isImmediateNext(shotId: string): boolean {
    if (!this.currentShotId) return false;
    const cur = this.shotIndexById.get(this.currentShotId);
    const cand = this.shotIndexById.get(shotId);
    return cur !== undefined && cand !== undefined && cand === cur + 1;
  }

  private preloadNext(): void {
    if (!this.currentShotId) return;
    const idx = this.shotIndexById.get(this.currentShotId);
    if (idx === undefined) return;
    const next = this.shots[idx + 1];
    const url = next ? this.clips.get(next.shot_id) : undefined;
    this.snapshot = { ...this.snapshot, preloadSrc: url ?? null };
  }

  private recomputeBuffer(): void {
    let committed = 0;
    const startIdx =
      this.currentShotId !== undefined && this.currentShotId !== null
        ? this.shotIndexById.get(this.currentShotId) ?? shotIndexForWord(this.shots, this.focusWord)
        : shotIndexForWord(this.shots, this.focusWord);
    if (startIdx !== null && startIdx !== undefined) {
      for (let i = startIdx; i < this.shots.length; i += 1) {
        if (this.clips.has(this.shots[i].shot_id)) committed += shotDuration(this.shots[i]);
        else break;
      }
    }
    const ahead = Math.max(0, committed - this.currentLocalTimeS);
    this.snapshot = { ...this.snapshot, committedSecondsAhead: ahead };
  }

  private scheduleIntent(): void {
    if (this.intentTimer) clearTimeout(this.intentTimer);
    this.intentTimer = setTimeout(() => {
      this.intentTimer = null;
      if (this.destroyed) return;
      this.pushIntentFn({
        focus_word: this.focusWord,
        velocity: this.velocityValue,
        mode: this.mode,
      });
    }, this.debounceMs);
  }

  private emit(): void {
    // Fold the always-derived scalars into a fresh snapshot reference so
    // useSyncExternalStore sees the change, then notify listeners.
    this.snapshot = {
      ...this.snapshot,
      owner: this.owner,
      mode: this.mode,
      focusWord: this.focusWord,
      velocity: this.velocityValue,
      currentShotId: this.currentShotId,
      budgetRemaining: this.budgetRemaining,
    };
    for (const listener of this.listeners) listener();
  }

  /** Exposed for tests / debugging. */
  get debug() {
    return {
      sessionId: this.sessionId,
      owner: this.owner,
      graceUntilMs: this.graceUntilMs,
    };
  }
}
