import type { SyncEngine } from "@kinora/core";
import { type FormEvent, useEffect, useRef, useState } from "react";

import type { SessionActivity } from "../../hooks/useSyncEngine";

interface CinemaPanelProps {
  engine: SyncEngine;
  clipUrl: string | null;
  isPlaying: boolean;
  mode: "viewer" | "director";
  onToggleMode: () => void;
  activity: SessionActivity[];
  budgetRemaining: number | null;
  onComment: (note: string) => void;
}

function fmt(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const KIND_DOT: Record<SessionActivity["kind"], string> = {
  agent: "bg-ember-glow",
  budget: "bg-amber-400",
  regen: "bg-sky-400",
  conflict: "bg-rose-400",
  scene: "bg-emerald-400",
};

/**
 * The film pane: the current shot's clip in a framed cinema surface with bespoke
 * transport (play/pause, scrub, mute) layered over the real frame-callback
 * playhead (`requestVideoFrameCallback`, falling back to `timeupdate`) that keeps
 * the karaoke highlight + page-turn frame-accurate. With no clip ready it shows
 * an intentional "rendering ahead" state. A slim footer carries the director
 * controls + live crew status (§5.6).
 */
export function CinemaPanel({
  engine,
  clipUrl,
  isPlaying,
  mode,
  onToggleMode,
  activity,
  budgetRemaining,
  onComment,
}: CinemaPanelProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [progress, setProgress] = useState(0);
  const [duration, setDuration] = useState(0);
  const [muted, setMuted] = useState(true);
  const [note, setNote] = useState("");

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !clipUrl) return;

    video.src = clipUrl;
    video.currentTime = 0;
    setProgress(0);
    void video.play().catch(() => {
      // Autoplay may be blocked until interaction; the transport lets the user start.
    });

    let cancelled = false;
    let handle = 0;
    const hasRvfc = typeof video.requestVideoFrameCallback === "function";

    const tick = (): void => {
      if (cancelled) return;
      engine.reportVideoTime(video.currentTime, performance.now());
      setProgress(video.currentTime);
      if (hasRvfc) handle = video.requestVideoFrameCallback(tick);
    };
    const onTimeUpdate = (): void => {
      engine.reportVideoTime(video.currentTime, performance.now());
      setProgress(video.currentTime);
    };
    const onMeta = (): void => setDuration(video.duration || 0);

    video.addEventListener("loadedmetadata", onMeta);
    if (hasRvfc) handle = video.requestVideoFrameCallback(tick);
    else video.addEventListener("timeupdate", onTimeUpdate);

    return () => {
      cancelled = true;
      video.removeEventListener("loadedmetadata", onMeta);
      if (hasRvfc && handle) video.cancelVideoFrameCallback(handle);
      else video.removeEventListener("timeupdate", onTimeUpdate);
    };
  }, [engine, clipUrl]);

  function togglePlay(): void {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) void video.play().catch(() => undefined);
    else video.pause();
  }

  function scrub(value: number): void {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = value;
    setProgress(value);
  }

  function toggleMute(): void {
    const video = videoRef.current;
    const next = !muted;
    setMuted(next);
    if (video) video.muted = next;
  }

  function submitNote(event: FormEvent): void {
    event.preventDefault();
    const trimmed = note.trim();
    if (!trimmed) return;
    onComment(trimmed);
    setNote("");
  }

  const latest = activity[0];
  const pct = duration > 0 ? (progress / duration) * 100 : 0;

  return (
    <div className="flex h-full min-h-0 flex-col bg-walnut-deep/60">
      {/* Stage */}
      <div className="relative flex min-h-0 flex-1 items-center justify-center p-6">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(80%_60%_at_50%_8%,rgba(224,134,58,0.12),transparent_60%)]" />
        <div className="glass-strong group relative aspect-video w-full max-w-3xl overflow-hidden rounded-glass">
          {/* The <video> stays mounted so the frame callback can attach; we just
              fade in the rendering state over it when there's no source. */}
          <video
            ref={videoRef}
            className={`h-full w-full bg-black object-contain transition-opacity duration-500 ${
              clipUrl ? "opacity-100" : "opacity-0"
            }`}
            playsInline
            muted={muted}
            onClick={togglePlay}
            onPlay={() => engine.setPlaying(true)}
            onPause={() => engine.setPlaying(false)}
          />

          {!clipUrl && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-5 bg-[radial-gradient(120%_100%_at_50%_0%,#1c130c,#0b0705_72%)] text-center">
              {/* Cinematic letterbox bars + a slow warm sweep, so the idle stage
                  reads as a film about to begin rather than an empty box. */}
              <div className="pointer-events-none absolute inset-x-0 top-0 h-[7%] bg-black/55" />
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-[7%] bg-black/55" />
              <div className="shimmer pointer-events-none absolute inset-0 opacity-60 motion-reduce:hidden" />

              {/* A soft ember beacon — two breathing rings around a still center,
                  signalling work without implying the clip is ready to play. */}
              <div className="relative flex h-16 w-16 items-center justify-center">
                <span className="absolute inset-0 animate-ping rounded-full bg-ember/25 [animation-duration:2.4s] motion-reduce:hidden" />
                <span className="absolute inset-2 animate-ping rounded-full bg-ember/20 [animation-duration:2.4s] [animation-delay:0.4s] motion-reduce:hidden" />
                <span className="relative flex h-10 w-10 items-center justify-center rounded-full bg-ember/25 text-ember-glow ring-1 ring-ember-glow/30">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M4 5h16v14H4z" />
                    <path d="M4 9h16M9 5 7 9M15 5l-2 4" />
                  </svg>
                </span>
              </div>
              <div className="px-6">
                <p className="font-display text-[17px] text-parchment">Rendering the next shot</p>
                <p className="mx-auto mt-1.5 max-w-[15rem] text-[13px] leading-relaxed text-white/45">
                  The crew is generating film a few seconds ahead of you.
                </p>
              </div>
            </div>
          )}

          {/* Transport — fades in on hover when a clip is present. */}
          {clipUrl && (
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

      {/* Director rail */}
      <div className="flex shrink-0 items-center gap-3 border-t border-white/10 px-5 py-3">
        <button
          type="button"
          onClick={onToggleMode}
          aria-pressed={mode === "director"}
          className={`flex h-8 shrink-0 items-center gap-1.5 rounded-full px-3 text-[12px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow ${
            mode === "director"
              ? "bg-ember text-walnut-deep"
              : "bg-white/8 text-white/75 hover:bg-white/16"
          }`}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 5h16v14H4zM4 9h16M9 5 7 9M15 5l-2 4" />
          </svg>
          {mode === "director" ? "Director" : "Viewer"}
        </button>

        {mode === "director" ? (
          <form onSubmit={submitNote} className="flex min-w-0 flex-1 items-center gap-2">
            <input
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Direct the scene — “warmer light, hold on her face”"
              className="glass-input min-w-0 flex-1 rounded-full px-3.5 py-1.5 text-[13px]"
            />
            <button
              type="submit"
              className="shrink-0 rounded-full bg-white/90 px-3.5 py-1.5 text-[12px] font-semibold text-walnut-deep transition hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
            >
              Send
            </button>
          </form>
        ) : (
          <div className="flex min-w-0 flex-1 items-center gap-2 text-[12px] text-white/45">
            {latest ? (
              <>
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${KIND_DOT[latest.kind]}`} />
                <span className="truncate">{latest.text}</span>
              </>
            ) : (
              <span className="truncate">The crew is standing by.</span>
            )}
          </div>
        )}

        {budgetRemaining !== null && (
          <span className="shrink-0 rounded-full bg-amber-400/15 px-2.5 py-1 text-[11px] font-medium text-amber-300">
            {Math.round(budgetRemaining)}s of film left
          </span>
        )}
      </div>
    </div>
  );
}
