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
import {
  buildTimeline,
  highlightedWordIndexAt,
  shotIndexForWord,
  shouldTurnPage,
  type TimelineShot,
} from "./timeline";
import { VelocityTracker, type VelocityOptions } from "./velocity";

export type ControlOwner = "scroll" | "video";
export type SessionMode = "viewer" | "director";

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
  currentClipUrl: string | null;
  currentPage: number;
  /** Global `word_index` to paint as the karaoke highlight, or null. */
  highlightWordIndex: number | null;
  isPlaying: boolean;
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
  private readonly segments = new Map<string, SyncSegment>();
  private readonly clipUrls = new Map<string, string>();
  private readonly velocityTracker: VelocityTracker;
  private readonly graceMs: number;
  private readonly intentDebounceMs: number;
  private readonly callbacks: SyncEngineCallbacks;

  private intentTimer: ReturnType<typeof setTimeout> | null = null;
  private ownerUntilMs = 0;
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
      currentClipUrl: null,
      currentPage: 0,
      highlightWordIndex: null,
      isPlaying: false,
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

  /** Load the book's shot list (the timeline the playhead resolves against). */
  setShots(shots: readonly ShotResponse[]): void {
    this.timeline = buildTimeline(shots);
    for (const shot of this.timeline) {
      if (shot.clipUrl) this.clipUrls.set(shot.shotId, shot.clipUrl);
    }
    this.resolveShotForWord(this.snapshot.focusWord);
  }

  /** Hot-swap: record a freshly rendered clip + its sync segment (clip_ready). */
  ingestClip(segment: SyncSegment, clipUrl?: string | null): void {
    this.segments.set(segment.shot_id, segment);
    if (clipUrl) this.clipUrls.set(segment.shot_id, clipUrl);
    if (clipUrl && segment.shot_id === this.snapshot.currentShotId) {
      this.emit({ currentClipUrl: clipUrl });
    }
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

  /** A deliberate jump to `word` (scrub/click) — resets velocity, seeks now. */
  seek(word: number, nowMs: number): void {
    this.takeOwnership("scroll", nowMs);
    this.velocityTracker.reset();
    this.emit({ focusWord: word, velocity: 0 });
    this.resolveShotForWord(word);
    this.callbacks.onSeek?.(word);
  }

  /** Playback advanced to `clipTimeS` within the current clip — drives read-along. */
  reportVideoTime(clipTimeS: number, nowMs: number): void {
    // Ignore playback updates during the grace window right after the reader scrolled.
    if (this.snapshot.owner === "scroll" && nowMs < this.ownerUntilMs) return;
    this.takeOwnership("video", nowMs);

    const shotId = this.snapshot.currentShotId;
    const segment = shotId ? this.segments.get(shotId) : undefined;
    if (!segment) return;

    const highlight = highlightedWordIndexAt(segment, clipTimeS);
    const page = shouldTurnPage(segment, clipTimeS) ? this.pageAfter(segment) : segment.page;
    const patch: Partial<SyncSnapshot> = { highlightWordIndex: highlight, currentPage: page };
    if (highlight !== null) patch.focusWord = highlight;
    this.emit(patch);
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
    if (!shot || shot.shotId === this.snapshot.currentShotId) return;
    this.emit({
      currentShotId: shot.shotId,
      currentClipUrl: this.clipUrls.get(shot.shotId) ?? null,
      currentPage: shot.page,
    });
  }

  private pageAfter(segment: SyncSegment): number {
    const index = this.timeline.findIndex((s) => s.shotId === segment.shot_id);
    const next = index >= 0 ? this.timeline[index + 1] : undefined;
    return next && next.page > segment.page ? next.page : segment.page;
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
