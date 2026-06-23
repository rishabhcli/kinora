import { shortShotId } from "@kinora/core";
import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useRef, useState } from "react";

import type { DirectorShot } from "./shots";

interface ShotTimelineProps {
  shots: DirectorShot[];
  currentShotId: string | null;
  onSeekShot: (shot: DirectorShot) => void;
  /** Playback fraction [0,1] of the shot on screen — drives the active tile's bar. */
  progressFraction?: number;
  /** shotId → number of directions the reader has given it (tile badge). */
  directionCounts?: Record<string, number>;
  /** Show shimmer skeletons instead of the empty state while shots load. */
  loading?: boolean;
}

function fmtDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  return `${seconds.toFixed(1)}s`;
}

/** The QA badge: pass/fail tint + the character-consistency score (§5.4 / §9.5). */
function QaBadge({ shot }: { shot: DirectorShot }) {
  if (shot.status === "regenerating") {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] font-medium text-sky-300">
        <span className="h-2.5 w-2.5 animate-spin rounded-full border border-sky-300/40 border-t-sky-300 motion-reduce:animate-none" />
        Rendering
      </span>
    );
  }
  const qa = shot.qa;
  if (!qa || qa.passed === null) {
    return (
      <span className="text-[10px] text-white/35">
        {shot.status === "pending" ? "Queued" : "No QA"}
      </span>
    );
  }
  const ccs = qa.ccs !== null ? qa.ccs.toFixed(2) : "—";
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] font-semibold ${
        qa.passed ? "text-emerald-300" : "text-rose-300"
      }`}
      title={`Character consistency ${ccs} — ${qa.passed ? "passed" : "failed"} QA`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${qa.passed ? "bg-emerald-400" : "bg-rose-400"}`} />
      CCS {ccs}
    </span>
  );
}

interface ShotTileProps {
  shot: DirectorShot;
  active: boolean;
  tabIndex: number;
  directionCount: number;
  progressFraction: number | null;
  onSeek: () => void;
  registerRef: (el: HTMLButtonElement | null) => void;
}

function ShotTile({
  shot,
  active,
  tabIndex,
  directionCount,
  progressFraction,
  onSeek,
  registerRef,
}: ShotTileProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);

  // Nudge the poster frame: a bare <video> often paints black until it decodes a
  // frame, so seek a hair past zero once data is in.
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !shot.clipUrl) return;
    const onData = (): void => {
      if (v.currentTime < 0.02) v.currentTime = 0.05;
    };
    v.addEventListener("loadeddata", onData);
    return () => v.removeEventListener("loadeddata", onData);
  }, [shot.clipUrl]);

  const directed = directionCount > 0;

  return (
    <button
      ref={registerRef}
      type="button"
      role="option"
      aria-selected={active}
      tabIndex={tabIndex}
      onClick={onSeek}
      aria-label={`Shot ${shot.sceneIndex} (${shortShotId(shot.shotId)})${
        directed ? `, ${directionCount} direction${directionCount === 1 ? "" : "s"} given` : ""
      } — seek here`}
      className={`group relative flex w-[132px] shrink-0 snap-start flex-col overflow-hidden rounded-xl text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow ${
        active ? "ring-2 ring-ember-glow" : "ring-1 ring-white/10 hover:ring-white/25"
      }`}
    >
      <div className="relative aspect-video w-full bg-black">
        {shot.clipUrl ? (
          <video
            ref={videoRef}
            src={shot.clipUrl}
            preload="metadata"
            muted
            playsInline
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="shimmer absolute inset-0 bg-walnut-deep/80" />
        )}

        {shot.status === "regenerating" && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/45 backdrop-blur-[1px]">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-sky-300/30 border-t-sky-300 motion-reduce:animate-none" />
          </div>
        )}

        {/* Directions-given badge (top-left). */}
        {directed && (
          <span
            className="absolute left-1 top-1 inline-flex items-center gap-0.5 rounded bg-ember/85 px-1 py-0.5 text-[9px] font-semibold text-walnut-deep"
            title={`${directionCount} direction${directionCount === 1 ? "" : "s"} given`}
          >
            <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
            </svg>
            {directionCount}
          </span>
        )}

        {/* Position pill + duration. */}
        <span className="absolute bottom-1 left-1 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-white/90">
          {shot.sceneIndex}
        </span>
        <span className="absolute bottom-1 right-1 rounded bg-black/60 px-1.5 py-0.5 text-[10px] tabular-nums text-white/75">
          {fmtDuration(shot.durationS)}
        </span>
        {active && <div className="pointer-events-none absolute inset-0 bg-ember/10" />}

        {/* Live playhead on the shot currently on screen. */}
        {active && progressFraction !== null && (
          <div className="absolute inset-x-0 bottom-0 h-0.5 bg-white/15">
            <div
              className="h-full bg-ember-glow transition-[width] duration-150 ease-linear"
              style={{ width: `${Math.min(100, Math.max(0, progressFraction * 100))}%` }}
            />
          </div>
        )}
      </div>

      <div className="flex items-center justify-between gap-1 bg-walnut-deep/70 px-2 py-1.5">
        <QaBadge shot={shot} />
        <span className="text-[10px] tabular-nums text-white/30">p{shot.page + 1}</span>
      </div>
    </button>
  );
}

/** Shimmer placeholders while the shot list loads. */
function SkeletonStrip() {
  return (
    <div className="flex gap-2.5 px-4 py-3" aria-hidden="true">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="w-[132px] shrink-0 overflow-hidden rounded-xl ring-1 ring-white/10">
          <div className="shimmer aspect-video w-full bg-walnut-deep/80" style={{ ["--shimmer-delay" as string]: `${i * 120}ms` }} />
          <div className="h-7 bg-walnut-deep/70" />
        </div>
      ))}
    </div>
  );
}

/**
 * The §5.4 shot timeline: a horizontal filmstrip of the current scene's shots.
 * Each tile carries a thumbnail, duration, QA badge, and a directions-given
 * badge; the one on screen is ringed, auto-scrolled into view, and shows a live
 * playhead bar. Fully keyboard-navigable (a roving-tabindex listbox): ←/→ move,
 * Home/End jump, Enter/Space seeks the playhead to that shot's word range.
 */
export function ShotTimeline({
  shots,
  currentShotId,
  onSeekShot,
  progressFraction,
  directionCounts = {},
  loading = false,
}: ShotTimelineProps) {
  const activeRef = useRef<HTMLButtonElement | null>(null);
  const tileRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const activeIndex = Math.max(0, shots.findIndex((s) => s.shotId === currentShotId));
  // The roving tab stop; follows the active shot but the reader can move it with
  // the arrows without seeking until they commit (Enter/Space/click).
  const [focusIndex, setFocusIndex] = useState(activeIndex);

  useEffect(() => {
    setFocusIndex(activeIndex);
  }, [activeIndex]);

  useEffect(() => {
    activeRef.current?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }, [currentShotId]);

  function focusTile(index: number): void {
    const clamped = Math.min(shots.length - 1, Math.max(0, index));
    setFocusIndex(clamped);
    tileRefs.current[clamped]?.focus();
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>): void {
    switch (event.key) {
      case "ArrowRight":
        event.preventDefault();
        focusTile(focusIndex + 1);
        break;
      case "ArrowLeft":
        event.preventDefault();
        focusTile(focusIndex - 1);
        break;
      case "Home":
        event.preventDefault();
        focusTile(0);
        break;
      case "End":
        event.preventDefault();
        focusTile(shots.length - 1);
        break;
      default:
        break;
    }
  }

  if (loading && shots.length === 0) return <SkeletonStrip />;

  if (shots.length === 0) {
    return (
      <div className="flex h-[132px] items-center justify-center px-4 text-[12px] text-white/40">
        No shots planned for this scene yet.
      </div>
    );
  }

  const renderingCount = shots.filter((s) => s.status === "regenerating").length;

  return (
    <div>
      {/* Scene header: how many shots are in the window + how many re-rendering. */}
      <div className="flex items-center justify-between px-4 pt-2 text-[10px] font-semibold uppercase tracking-wider text-white/35">
        <span>
          {shots.length} shot{shots.length === 1 ? "" : "s"} in scene
        </span>
        {renderingCount > 0 && (
          <span className="inline-flex items-center gap-1 normal-case text-sky-300">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400 motion-reduce:animate-none" />
            {renderingCount} rendering
          </span>
        )}
      </div>

      <div
        role="listbox"
        aria-label="Shot timeline"
        aria-orientation="horizontal"
        onKeyDown={onKeyDown}
        className="flex snap-x gap-2.5 overflow-x-auto px-4 pb-3 pt-2 [scrollbar-width:thin] focus:outline-none"
      >
        {shots.map((shot, index) => {
        const active = shot.shotId === currentShotId;
        return (
          <ShotTile
            key={shot.shotId}
            shot={shot}
            active={active}
            tabIndex={index === focusIndex ? 0 : -1}
            directionCount={directionCounts[shot.shotId] ?? 0}
            progressFraction={active ? (progressFraction ?? null) : null}
            onSeek={() => onSeekShot(shot)}
            registerRef={(el) => {
              tileRefs.current[index] = el;
              if (active) activeRef.current = el;
            }}
          />
        );
        })}
      </div>
    </div>
  );
}
