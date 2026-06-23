import type {
  BeatStage,
  CommentResponse,
  PlaybackSource,
  SessionActivity,
  SocketStatus,
  SyncEngine,
} from "@kinora/core";
import { useCallback, useEffect, useRef, useState } from "react";

import { DirectorRail, type CommentRoute } from "../director/DirectorRail";
import { RegionMarker, RegionSelectOverlay } from "../director/RegionSelectOverlay";
import { exportRegionPng, type NormBox } from "../director/regionCapture";
import type { DirectorShot } from "../director/shots";
import type { DirectionEntry } from "../../hooks/useDirectorHistory";
import { useIdlePause } from "../../hooks/useIdlePause";
import { DegradedStage } from "./DegradedStage";

interface CinemaPanelProps {
  engine: SyncEngine;
  /**
   * The active video source URL: a stitched scene mp4 when one covers the
   * playhead (then constant across the whole scene, so the double-buffer never
   * swaps mid-scene — playback is gapless), else the per-shot clip (§9.6).
   */
  clipUrl: string | null;
  /** The active source's id (scene_id or shot_id) — tags `reportVideoTime` so the engine reads the right time base (§9.6). */
  sourceId: string | null;
  /** Absolute time to jump the active asset to on a deliberate seek / scene hot-swap (§4.8); applied whenever `playheadSeekSeq` changes. */
  playheadSeekS: number | null;
  playheadSeekSeq: number;
  /** The next playable source — warmed in the hidden buffer for an instant, gapless boundary swap (§5.2/§9.6). */
  nextSource: PlaybackSource | null;
  /** The active §12.4 ladder rung for the beat on screen. */
  stage: BeatStage;
  /** The current beat's keyframe still, when generated (the Ken-Burns bridge). */
  keyframeUrl: string | null;
  /** The book's own page image for the beat (the deep fallback rung). */
  illustrationUrl: string | null;
  /** The current beat id — seeds the deterministic Ken-Burns motion. */
  beatId: string | null;
  underBudgetPressure: boolean;
  isPlaying: boolean;
  mode: "viewer" | "director";
  onToggleMode: () => void;
  socketStatus: SocketStatus;
  budgetRemaining: number | null;
  // Director mode (§5.4)
  activity: SessionActivity[];
  sceneShots: DirectorShot[];
  currentShotId: string | null;
  onSeekShot: (shot: DirectorShot) => void;
  /** Region-comment via the regen-triggering REST path; resolves with routing. */
  onSendComment: (note: string, regionPng?: string | null) => Promise<CommentResponse | null>;
  /** shotId → directions-given count (timeline tile badges). */
  directionCounts?: Record<string, number>;
  /** Directions already given to the shot on screen (newest first). */
  directions?: DirectionEntry[];
  /** Whether the shot list is still loading (timeline skeletons). */
  loadingShots?: boolean;
}

function fmt(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** The footer's ambient link indicator (the feed owns the full activity log). */
const LINK_META: Record<SocketStatus, { label: string; dot: string; live: boolean }> = {
  open: { label: "Live", dot: "text-emerald-400", live: true },
  connecting: { label: "Reconnecting", dot: "text-amber-400", live: true },
  closed: { label: "Offline", dot: "text-white/35", live: false },
};

/** The cross-fade overlap, ms — also how long we keep the outgoing clip alive. */
const SWAP_FADE_MS = 320;

/** The liquid-glass Viewer | Director switch that flips the right pane (§5.2). */
function ModeSwitch({ mode, onToggle }: { mode: "viewer" | "director"; onToggle: () => void }) {
  return (
    <div className="glass-strong absolute right-4 top-4 z-30 flex items-center gap-0.5 rounded-full p-0.5 text-[11.5px] font-medium">
      {(["viewer", "director"] as const).map((m) => (
        <button
          key={m}
          type="button"
          onClick={() => m !== mode && onToggle()}
          aria-pressed={mode === m}
          className={`rounded-full px-3 py-1 capitalize transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow ${
            mode === m ? "bg-ember text-walnut-deep" : "text-white/70 hover:text-white"
          }`}
        >
          {m}
        </button>
      ))}
    </div>
  );
}

/**
 * The film pane: the current shot's clip on a framed cinema surface with bespoke
 * transport (play/pause, scrub, mute) over the real frame-callback playhead
 * (`requestVideoFrameCallback`, falling back to `timeupdate`) that keeps the
 * karaoke highlight + page-turn frame-accurate.
 *
 * It implements the §12.4 ladder on the client. When the committed clip isn't on
 * screen yet it bridges with a {@link DegradedStage} (a Ken-Burns'd keyframe /
 * illustration, or the audio-text floor) — never a spinner. Incoming clips are
 * **double-buffered**: the new source preloads into a hidden second `<video>` and
 * is promoted on its first decoded frame (a clean boundary), so the visible film
 * never flashes black mid-`src`-mutation.
 *
 * Director mode (§5.4) overlays a region-select scrim on the stage and swaps the
 * footer for the Director rail (shot timeline · region-comment composer · feed).
 */
export function CinemaPanel({
  engine,
  clipUrl,
  sourceId,
  playheadSeekS,
  playheadSeekSeq,
  nextSource,
  stage,
  keyframeUrl,
  illustrationUrl,
  beatId,
  underBudgetPressure,
  isPlaying,
  mode,
  onToggleMode,
  socketStatus,
  budgetRemaining,
  activity,
  sceneShots,
  currentShotId,
  onSeekShot,
  onSendComment,
  directionCounts,
  directions,
  loadingShots,
}: CinemaPanelProps) {
  const videoARef = useRef<HTMLVideoElement | null>(null);
  const videoBRef = useRef<HTMLVideoElement | null>(null);
  // §4.7 idle-pause: rest the Ken-Burns pan (and playing film) when the room is
  // idle or the tab is hidden; resume on return.
  const idle = useIdlePause(engine);
  // Which buffer is the visible, reporting one; `activeUrl` is the clip on it.
  const [activeBuf, setActiveBuf] = useState<"A" | "B">("A");
  const [activeUrl, setActiveUrl] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const [duration, setDuration] = useState(0);
  const [muted, setMuted] = useState(true);

  // §4.8 deliberate-seek signal from the engine, read without re-subscribing the
  // preload effect; `applied` tracks the last seq we have honoured.
  const seekRef = useRef<{ s: number | null; seq: number }>({ s: null, seq: 0 });
  seekRef.current = { s: playheadSeekS, seq: playheadSeekSeq };
  const appliedSeekSeqRef = useRef(0);
  // The source id the active buffer is playing, so `reportVideoTime` tags the
  // right time base (absolute for a scene, clip-local for a shot, §9.6).
  const sourceIdRef = useRef<string | null>(sourceId);
  sourceIdRef.current = sourceId;
  // The src each buffer currently holds, so a proactively warmed `nextSource`
  // (preloaded into the idle buffer ahead of the boundary) is promoted without a
  // re-fetch — the boundary swap is then instant, not a stutter.
  const loadedRef = useRef<{ A: string | null; B: string | null }>({ A: null, B: null });

  // Director region-select state.
  const [armed, setArmed] = useState(false);
  const [region, setRegion] = useState<{ png: string | null; box: NormBox } | null>(null);
  const [sending, setSending] = useState(false);
  const [route, setRoute] = useState<CommentRoute | null>(null);

  // A bound region (and its PNG) is tied to the shot it was drawn on; seeking to
  // another shot — a timeline click or playback advancing — clears it so a note
  // can never target the wrong frame, and retires the stale routing chip.
  useEffect(() => {
    setRegion(null);
    setArmed(false);
    setRoute(null);
  }, [currentShotId]);

  const refFor = useCallback((buf: "A" | "B") => (buf === "A" ? videoARef : videoBRef), []);
  const activeVideo = useCallback(() => refFor(activeBuf).current, [activeBuf, refFor]);

  // While the committed clip for this beat isn't actually on the visible buffer,
  // hold the moment with the degraded bridge (also covers first-clip preload).
  const showBridge = stage !== "full_video" || activeUrl !== clipUrl;

  // Director keyboard shortcuts (§5.4): R arms region-select, Esc cancels/clears.
  // Ignored while typing so the composer keeps its own keys (it owns ⌘/Ctrl+↵).
  useEffect(() => {
    if (mode !== "director") return;
    const onKey = (e: KeyboardEvent): void => {
      const el = e.target as HTMLElement | null;
      const typing =
        !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
      if (e.key === "Escape") {
        setArmed(false);
        setRegion(null);
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if ((e.key === "r" || e.key === "R") && !showBridge) {
        e.preventDefault();
        setArmed((a) => !a);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, showBridge]);

  // --- Double-buffer: preload the incoming clip, promote on a clean frame. --- #
  useEffect(() => {
    if (!clipUrl || clipUrl === activeUrl) return;
    const incomingBuf = activeBuf === "A" ? "B" : "A";
    const incoming = refFor(incomingBuf).current;
    const outgoing = activeVideo();
    if (!incoming) return;

    // A deliberate seek or a scene hot-swap that lands on this new source
    // positions it at the word's frame (§4.8/§9.6) — e.g. swapping per-shot
    // playback for the stitched scene mid-scene continues from the same moment.
    // A natural boundary change (the next shot/scene) carries no seek → start at 0.
    const { s: seekS, seq: seekSeq } = seekRef.current;
    const seekPending = seekSeq !== appliedSeekSeqRef.current && seekS != null;
    if (seekPending) appliedSeekSeqRef.current = seekSeq;
    const startAt = seekPending && seekS != null && seekS > 0 ? seekS : 0;

    // Skip the re-fetch if this buffer was already warmed with the source by the
    // proactive `nextSource` preload below — that is what makes the swap instant.
    const warm = loadedRef.current[incomingBuf] === clipUrl;
    if (!warm) {
      incoming.src = clipUrl;
      incoming.load();
      loadedRef.current[incomingBuf] = clipUrl;
    }

    let promoted = false;
    let handle = 0;
    const hasRvfc = typeof incoming.requestVideoFrameCallback === "function";
    const promote = (): void => {
      if (promoted) return;
      promoted = true;
      setActiveBuf((prev) => (prev === "A" ? "B" : "A"));
      setActiveUrl(clipUrl);
      setProgress(startAt);
      // Pause the outgoing immediately so two narrations never overlap (no audio
      // pop); its last frame stays visible and cross-fades out via opacity.
      if (outgoing && outgoing !== incoming) outgoing.pause();
      void incoming
        .play()
        .then(() => engine.setPlaying(true))
        .catch(() => {
          // Autoplay may be blocked until interaction; transport lets them start.
          engine.setPlaying(false);
        });
      // Safety: ensure the retired buffer is fully stopped after the cross-fade.
      window.setTimeout(() => {
        if (outgoing && outgoing !== incoming) outgoing.pause();
      }, SWAP_FADE_MS);
    };

    let onMeta: (() => void) | null = null;
    if (startAt > 0) {
      // Seek the hidden buffer to the target first, then promote on the sought
      // frame (`seeked`) so the swap lands exactly there with no black flash.
      onMeta = () => {
        try {
          incoming.currentTime = startAt;
        } catch {
          /* not seekable yet */
        }
      };
      if (incoming.readyState >= 1) onMeta();
      else incoming.addEventListener("loadedmetadata", onMeta, { once: true });
      incoming.addEventListener("seeked", promote, { once: true });
      incoming.addEventListener("canplay", promote, { once: true }); // fallback
    } else {
      incoming.currentTime = 0;
      if (warm && incoming.readyState >= 2) {
        // Already warmed + decoded → swap right now, no wait or stutter.
        promote();
      } else if (hasRvfc) {
        // The first decoded frame is the clean boundary to swap on.
        handle = incoming.requestVideoFrameCallback(() => promote());
      } else {
        incoming.addEventListener("canplay", promote, { once: true });
      }
    }

    return () => {
      if (onMeta) incoming.removeEventListener("loadedmetadata", onMeta);
      incoming.removeEventListener("seeked", promote);
      incoming.removeEventListener("canplay", promote);
      if (hasRvfc && handle) incoming.cancelVideoFrameCallback(handle);
    };
  }, [clipUrl, activeUrl, activeBuf, engine, refFor, activeVideo]);

  // --- Proactively warm the idle buffer with nextSource (§5.2/§9.6). --------- #
  // Loading the upcoming scene/shot before the boundary makes the swap instant
  // (the main effect promotes it without a re-fetch — see `warm`). Only while
  // stably playing the active source, so it never disturbs an in-flight swap.
  useEffect(() => {
    const url = nextSource?.url;
    if (!url || url === clipUrl || clipUrl !== activeUrl) return;
    const idleBuf = activeBuf === "A" ? "B" : "A";
    if (loadedRef.current[idleBuf] === url) return;
    const idle = refFor(idleBuf).current;
    if (!idle) return;
    idle.src = url;
    idle.load();
    loadedRef.current[idleBuf] = url;
  }, [nextSource?.url, clipUrl, activeUrl, activeBuf, refFor]);

  // --- A deliberate seek within the *same* source (§4.8): jump in place. ----- #
  // (A seek that also changes the source is handled by the preload effect above.)
  useEffect(() => {
    if (playheadSeekSeq === appliedSeekSeqRef.current) return;
    if (clipUrl !== activeUrl) return; // a source swap is in flight — let it seek
    appliedSeekSeqRef.current = playheadSeekSeq;
    if (playheadSeekS == null) return;
    const video = activeVideo();
    if (!video) return;
    const apply = (): void => {
      try {
        video.currentTime = playheadSeekS;
      } catch {
        /* not seekable yet */
      }
      setProgress(playheadSeekS);
    };
    if (video.readyState >= 1) apply();
    else video.addEventListener("loadedmetadata", apply, { once: true });
  }, [playheadSeekSeq, playheadSeekS, clipUrl, activeUrl, activeVideo]);

  // --- Drive the playhead from whichever buffer is active + visible. -------- #
  useEffect(() => {
    const video = refFor(activeBuf).current;
    if (!video || !activeUrl) return;

    let cancelled = false;
    let handle = 0;
    const hasRvfc = typeof video.requestVideoFrameCallback === "function";
    const tick = (): void => {
      if (cancelled) return;
      engine.reportVideoTime(video.currentTime, performance.now(), sourceIdRef.current ?? undefined);
      setProgress(video.currentTime);
      if (hasRvfc) handle = video.requestVideoFrameCallback(tick);
    };
    const onTimeUpdate = (): void => {
      engine.reportVideoTime(video.currentTime, performance.now(), sourceIdRef.current ?? undefined);
      setProgress(video.currentTime);
    };
    const onMeta = (): void => setDuration(video.duration || 0);
    const onEnded = (): void => {
      // Flow continuously into the preloaded next source (§9.6): advancing flips
      // currentClipUrl → the warmed idle buffer promotes instantly. Nothing
      // queued (end of book / buffer-starved) → just stop.
      if (!engine.advanceToNextSource()) engine.setPlaying(false);
    };

    video.addEventListener("loadedmetadata", onMeta);
    video.addEventListener("ended", onEnded);
    if (hasRvfc) handle = video.requestVideoFrameCallback(tick);
    else video.addEventListener("timeupdate", onTimeUpdate);

    return () => {
      cancelled = true;
      video.removeEventListener("loadedmetadata", onMeta);
      video.removeEventListener("ended", onEnded);
      if (hasRvfc && handle) video.cancelVideoFrameCallback(handle);
      else video.removeEventListener("timeupdate", onTimeUpdate);
    };
  }, [engine, activeBuf, activeUrl, refFor]);

  // When the playhead steps off the committed rung (reader scrolled to an
  // uncommitted beat), quiet the film under the bridge rather than play on blind.
  useEffect(() => {
    if (stage !== "full_video") {
      videoARef.current?.pause();
      videoBRef.current?.pause();
      engine.setPlaying(false);
    }
  }, [stage, engine]);

  // §4.7 idle-pause: pause a playing film when the room goes idle / the tab hides,
  // and resume it when the reader returns — so a backgrounded tab burns nothing.
  const wasPlayingRef = useRef(false);
  useEffect(() => {
    const video = activeVideo();
    if (idle) {
      wasPlayingRef.current = !!video && !video.paused;
      videoARef.current?.pause();
      videoBRef.current?.pause();
      if (wasPlayingRef.current) engine.setPlaying(false);
    } else if (wasPlayingRef.current) {
      wasPlayingRef.current = false;
      void video
        ?.play()
        .then(() => engine.setPlaying(true))
        .catch(() => undefined);
    }
  }, [idle, activeVideo, engine]);

  function togglePlay(): void {
    const video = activeVideo();
    if (!video) return;
    if (video.paused) {
      void video
        .play()
        .then(() => engine.setPlaying(true))
        .catch(() => undefined);
    } else {
      video.pause();
      engine.setPlaying(false);
    }
  }

  function scrub(value: number): void {
    const video = activeVideo();
    if (!video) return;
    video.currentTime = value;
    setProgress(value);
  }

  function toggleMute(): void {
    const next = !muted;
    setMuted(next);
    if (videoARef.current) videoARef.current.muted = next;
    if (videoBRef.current) videoBRef.current.muted = next;
  }

  // A buffer's source URL failed to load (an expired presigned URL past its TTL,
  // or a network error) — drop that source so the engine re-resolves to the
  // next-best rung instead of freezing, and forget the dead buffer so it is never
  // promoted as "warm".
  function handleVideoError(buf: "A" | "B"): void {
    const url = loadedRef.current[buf];
    loadedRef.current[buf] = null;
    if (!url) return;
    if (url === clipUrl && sourceId) engine.markSourceFailed(sourceId);
    else if (nextSource && url === nextSource.url) engine.markSourceFailed(nextSource.id);
  }

  // Keyboard transport on the focused stage: space toggles play, arrows seek ±5s
  // within the active source, m mutes. Returns whether it handled the key.
  function handleTransportKey(key: string): boolean {
    const video = activeVideo();
    if (key === " " || key === "Spacebar") {
      togglePlay();
      return true;
    }
    if (key === "ArrowRight" && video) {
      scrub(Math.min(video.duration || video.currentTime, video.currentTime + 5));
      return true;
    }
    if (key === "ArrowLeft" && video) {
      scrub(Math.max(0, video.currentTime - 5));
      return true;
    }
    if (key === "m" || key === "M") {
      toggleMute();
      return true;
    }
    return false;
  }

  // A box was drawn: best-effort screenshot of the region, bound to this shot.
  async function handleRegionSelect(box: NormBox): Promise<void> {
    setArmed(false);
    const t = activeVideo()?.currentTime ?? 0;
    const png = clipUrl ? await exportRegionPng(clipUrl, t, box) : null;
    setRegion({ png, box });
  }

  function clearRegion(): void {
    setRegion(null);
    setArmed(false);
  }

  async function handleSend(note: string): Promise<CommentRoute | null> {
    setSending(true);
    try {
      const res = await onSendComment(note, region?.png ?? null);
      const next: CommentRoute | null = res
        ? {
            agent: res.agent,
            aspect: res.aspect,
            message: res.message,
            learned: res.learned?.map((l) => ({ label: l.label, applied: l.applied })),
          }
        : null;
      setRoute(next);
      if (res) {
        setRegion(null);
        setArmed(false);
      }
      return next;
    } finally {
      setSending(false);
    }
  }

  const pct = duration > 0 ? (progress / duration) * 100 : 0;
  const link = LINK_META[socketStatus];
  // The bridge shows the best still we hold; keyframe outranks the illustration.
  const bridgeStill = keyframeUrl ?? illustrationUrl;
  const bridgeVariant = keyframeUrl ? "keyframe" : illustrationUrl ? "illustration" : "audio_text";
  const canArm = !showBridge; // region-select only over a committed clip on screen

  return (
    <div className="flex h-full min-h-0 flex-col bg-walnut-deep/60">
      {/* Stage */}
      <div className="relative flex min-h-0 flex-1 items-center justify-center p-6">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(80%_60%_at_50%_8%,rgba(224,134,58,0.12),transparent_60%)]" />
        <div
          className="glass-strong group relative aspect-video w-full max-w-3xl overflow-hidden rounded-glass focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow/60"
          tabIndex={0}
          role="application"
          aria-label="Film player — space to play or pause, arrow keys to seek, m to mute"
          onKeyDown={(event) => {
            if (handleTransportKey(event.key)) event.preventDefault();
          }}
        >
          {/* Double-buffered film: two stacked elements, only the active one is
              visible + reporting. The new source preloads into the hidden one and
              we cross-fade on its first frame — no black flash on src mutation. */}
          <video
            ref={videoARef}
            className={`absolute inset-0 h-full w-full bg-black object-contain transition-opacity duration-300 motion-reduce:transition-none ${
              activeBuf === "A" && activeUrl && !showBridge ? "opacity-100" : "opacity-0"
            }`}
            playsInline
            muted={muted}
            onClick={togglePlay}
            onError={() => handleVideoError("A")}
          />
          <video
            ref={videoBRef}
            className={`absolute inset-0 h-full w-full bg-black object-contain transition-opacity duration-300 motion-reduce:transition-none ${
              activeBuf === "B" && activeUrl && !showBridge ? "opacity-100" : "opacity-0"
            }`}
            playsInline
            muted={muted}
            onClick={togglePlay}
            onError={() => handleVideoError("B")}
          />

          {/* The §12.4 bridge — Ken-Burns over a still, or the audio/text floor. */}
          {showBridge && (
            <DegradedStage
              stillUrl={bridgeStill}
              variant={bridgeVariant}
              seed={beatId}
              budgetRemaining={budgetRemaining}
              underBudgetPressure={underBudgetPressure}
              engine={engine}
              paused={idle}
            />
          )}

          {/* Region-select scrim (Director mode, armed, over a committed clip). */}
          {mode === "director" && armed && canArm && (
            <RegionSelectOverlay
              video={activeVideo()}
              onSelect={handleRegionSelect}
              onCancel={() => setArmed(false)}
            />
          )}

          {/* The bound region stays marked on the stage until sent or cleared. */}
          {mode === "director" && !armed && canArm && region && (
            <RegionMarker video={activeVideo()} box={region.box} />
          )}

          <ModeSwitch mode={mode} onToggle={onToggleMode} />

          {/* Transport — over a committed clip on screen, hidden while arming. */}
          {!showBridge && activeUrl && !(mode === "director" && armed) && (
            <div className="absolute inset-x-0 bottom-0 flex items-center gap-3 bg-gradient-to-t from-black/75 to-transparent px-4 pb-3.5 pt-10 opacity-0 transition-opacity duration-200 group-hover:opacity-100 focus-within:opacity-100">
              <button
                type="button"
                aria-label={isPlaying ? "Pause" : "Play"}
                onClick={togglePlay}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white/90 text-walnut-deep transition hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
              >
                {isPlaying ? (
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5h4v14H7zM13 5h4v14h-4z" /></svg>
                ) : (
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l11-6.5-11-6.5Z" /></svg>
                )}
              </button>
              <span className="w-9 shrink-0 font-sans text-[11px] tabular-nums text-white/70">{fmt(progress)}</span>
              <input
                type="range"
                min={0}
                max={Math.max(duration, 0.1)}
                step={0.05}
                value={Math.min(progress, duration || progress)}
                aria-label="Scrub film"
                onChange={(event) => scrub(Number(event.target.value))}
                className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-white/20 accent-ember-glow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow/60"
                style={{ background: `linear-gradient(90deg, rgba(244,168,93,0.9) ${pct}%, rgba(255,255,255,0.2) ${pct}%)` }}
              />
              <span className="w-9 shrink-0 font-sans text-[11px] tabular-nums text-white/70">{fmt(duration)}</span>
              <button
                type="button"
                aria-label={muted ? "Unmute" : "Mute"}
                onClick={toggleMute}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-white/80 transition hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
              >
                {muted ? (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="m17 9 4 6M21 9l-4 6" /></svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="M16 9a4 4 0 0 1 0 6" /><path d="M19 6.5a8 8 0 0 1 0 11" /></svg>
                )}
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Footer — the Director rail (§5.4) or the ambient viewer status strip. */}
      {mode === "director" ? (
        <DirectorRail
          shots={sceneShots}
          currentShotId={currentShotId}
          onSeekShot={onSeekShot}
          armed={armed}
          canArm={canArm}
          onArmToggle={() => setArmed((a) => !a)}
          region={region}
          onClearRegion={clearRegion}
          onSend={handleSend}
          sending={sending}
          route={route}
          activity={activity}
          budgetRemaining={budgetRemaining}
          progressFraction={duration > 0 ? progress / duration : 0}
          directionCounts={directionCounts}
          directions={directions}
          loadingShots={loadingShots}
        />
      ) : (
        <div className="flex shrink-0 items-center gap-3 border-t border-white/10 px-5 py-3">
          <div className={`flex min-w-0 flex-1 items-center gap-1.5 text-[12px] ${link.dot}`}>
            <span className="status-pulse" data-live={link.live} aria-hidden="true" />
            <span className="truncate text-white/45">{link.label}</span>
          </div>
          {budgetRemaining !== null && (
            <span className="shrink-0 rounded-full bg-amber-400/15 px-2.5 py-1 text-[11px] font-medium text-amber-300">
              {Math.round(budgetRemaining)}s of film left
            </span>
          )}
        </div>
      )}
    </div>
  );
}
