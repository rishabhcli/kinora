import { type KeyboardEvent, useEffect, useMemo, useState } from "react";

import { useReducedMotion } from "../hooks/useReducedMotion";
import {
  beats,
  clampIndex,
  EXCERPT_BYLINE,
  EXCERPT_TITLE,
  tokenize,
  zoneForOffset,
} from "../lib/preview";

const STEP_MS = 260;

function PlayIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
      <path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.7-6.86a1 1 0 0 0 0-1.68L9.54 4.3A1 1 0 0 0 8 5.14Z" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
      <path d="M7 4h3v16H7zM14 4h3v16h-3z" />
    </svg>
  );
}

function RestartIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M3 12a9 9 0 1 0 3-6.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M3 4v4h4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function zoneBarClass(zone: ReturnType<typeof zoneForOffset>): string {
  switch (zone) {
    case "played":
      return "bg-kinora-glow/80";
    case "playing":
      return "bg-kinora-glow motion-safe:animate-pulse-glow";
    case "committed":
      return "bg-kinora-glow/55";
    case "speculative":
      return "bg-transparent ring-1 ring-inset ring-kinora-iris/70";
    case "cold":
    default:
      return "bg-kinora-line";
  }
}

function ReaderPreview() {
  const reducedMotion = useReducedMotion();
  const tokens = useMemo(() => tokenize(), []);
  const total = tokens.length;

  const [focus, setFocus] = useState(0);
  const [playing, setPlaying] = useState(false);

  const currentBeat = tokens[focus]?.beat ?? 0;
  const activeBeat = beats[currentBeat] ?? beats[0];
  const atEnd = focus >= total - 1;
  const progress = total > 0 ? (focus + 1) / total : 0;

  useEffect(() => {
    if (!playing) return undefined;
    if (focus >= total - 1) {
      setPlaying(false);
      return undefined;
    }
    const id = window.setTimeout(() => setFocus((f) => clampIndex(f + 1, total)), STEP_MS);
    return () => window.clearTimeout(id);
  }, [playing, focus, total]);

  const toggle = () => {
    if (atEnd && !playing) {
      setFocus(0);
      setPlaying(true);
    } else {
      setPlaying((p) => !p);
    }
  };
  const restart = () => setFocus(0);
  const step = (delta: number) => {
    setPlaying(false);
    setFocus((f) => clampIndex(f + delta, total));
  };
  const seek = (index: number) => {
    setPlaying(false);
    setFocus(clampIndex(index, total));
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    switch (event.key) {
      case " ":
      case "k":
        event.preventDefault();
        toggle();
        break;
      case "ArrowRight":
      case "l":
        event.preventDefault();
        step(1);
        break;
      case "ArrowLeft":
      case "j":
        event.preventDefault();
        step(-1);
        break;
      case "Home":
        event.preventDefault();
        seek(0);
        break;
      case "End":
        event.preventDefault();
        seek(total - 1);
        break;
      default:
        break;
    }
  };

  const playLabel = atEnd && !playing ? "Replay preview" : playing ? "Pause preview" : "Play preview";

  return (
    <div className="overflow-hidden rounded-3xl border border-kinora-line bg-kinora-panel/70 shadow-2xl backdrop-blur">
      <div className="flex items-center justify-between gap-3 border-b border-kinora-line/80 px-5 py-3 text-xs text-kinora-muted">
        <span className="inline-flex items-center gap-2 font-medium">
          <span className="flex gap-1" aria-hidden="true">
            <span className="h-2.5 w-2.5 rounded-full bg-kinora-line" />
            <span className="h-2.5 w-2.5 rounded-full bg-kinora-line" />
            <span className="h-2.5 w-2.5 rounded-full bg-kinora-glow/70" />
          </span>
          Two-pane reading workspace
        </span>
        <span className="hidden sm:inline">Illustrative on-device preview</span>
      </div>

      <div
        role="group"
        aria-label="Reading preview. Use the left and right arrow keys to move word by word, and space to play or pause."
        tabIndex={0}
        onKeyDown={onKeyDown}
        className="grid grid-cols-1 gap-px bg-kinora-line/60 md:grid-cols-2"
      >
        <div className="bg-kinora-panel/90 p-5 sm:p-6">
          <div className="mb-3 flex items-center justify-between text-[0.7rem] uppercase tracking-[0.2em] text-kinora-muted">
            <span>{EXCERPT_TITLE}</span>
            <span aria-hidden="true">Page 1</span>
          </div>
          <p className="text-lg leading-relaxed text-kinora-muted sm:text-xl">
            {tokens.map((token, index) => {
              const state =
                index === focus ? "current" : index < focus ? "read" : "ahead";
              const className =
                state === "current"
                  ? "rounded bg-kinora-glow px-1 text-white shadow-[0_0_18px_rgba(124,92,255,0.45)]"
                  : state === "read"
                    ? "text-kinora-mist"
                    : "text-kinora-muted";
              return (
                <span
                  key={index}
                  onClick={() => seek(index)}
                  aria-current={index === focus ? "true" : undefined}
                  className={`cursor-pointer rounded px-0.5 transition-colors ${className}`}
                >
                  {token.word}{" "}
                </span>
              );
            })}
          </p>
          <p className="mt-4 text-xs italic text-kinora-muted/80">{EXCERPT_BYLINE}</p>
        </div>

        <div
          role="img"
          aria-label={`Illustrative film shot ${currentBeat + 1} of ${beats.length}: ${activeBeat.shot}`}
          className="relative min-h-[15rem] overflow-hidden bg-black sm:min-h-[17rem]"
        >
          {beats.map((beat) => {
            const isActive = beat.id === currentBeat;
            return (
              <div
                key={beat.id}
                aria-hidden="true"
                className={
                  reducedMotion
                    ? "absolute inset-0"
                    : "absolute inset-0 transition-opacity duration-700"
                }
                style={{ opacity: isActive ? 1 : 0 }}
              >
                <div
                  className={
                    isActive && !reducedMotion
                      ? "absolute inset-0 motion-safe:animate-ken-burns"
                      : "absolute inset-0"
                  }
                  style={{
                    background: `radial-gradient(120% 85% at 26% 16%, ${beat.scene.orb}, transparent 55%), linear-gradient(180deg, ${beat.scene.skyTop}, ${beat.scene.skyBottom})`,
                  }}
                >
                  <div
                    className="absolute left-[14%] top-[12%] h-16 w-16 rounded-full blur-md"
                    style={{ background: beat.scene.orb, opacity: 0.85 }}
                  />
                  <div
                    className="absolute -inset-x-8 bottom-[-32%] h-[70%] rounded-[50%]"
                    style={{ background: beat.scene.ground }}
                  />
                  {beat.scene.motion ? (
                    <div className="absolute right-[10%] top-1/2 h-1 w-1/2 -translate-y-1/2 rounded-full bg-white/70 blur-[2px]" />
                  ) : null}
                </div>
              </div>
            );
          })}

          <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-black/10" aria-hidden="true" />

          <div className="pointer-events-none absolute inset-0 flex items-center justify-center" aria-hidden="true">
            <span className="flex h-14 w-14 items-center justify-center rounded-full border border-white/50 bg-black/45 text-white backdrop-blur-sm">
              {playing ? <PauseIcon /> : <PlayIcon />}
            </span>
          </div>

          <div className="absolute inset-x-0 bottom-0 p-4">
            <p className="text-sm font-medium text-white drop-shadow">
              <span className="text-kinora-iris">Shot {String(currentBeat + 1).padStart(2, "0")}</span>{" "}
              <span className="text-white/60">/ {String(beats.length).padStart(2, "0")}</span> —{" "}
              {activeBeat.shot}
            </p>
            <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-white/20">
              <div
                className={reducedMotion ? "h-full bg-kinora-iris" : "h-full bg-kinora-iris transition-[width] duration-200"}
                style={{ width: `${Math.round(progress * 100)}%` }}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-kinora-line/80 p-5">
        <button
          type="button"
          onClick={toggle}
          className="inline-flex items-center gap-2 rounded-full bg-[#6d28d9] px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-[#7c5cff] focus-visible:ring-2 focus-visible:ring-kinora-iris focus-visible:ring-offset-2 focus-visible:ring-offset-kinora-panel"
        >
          {playing ? <PauseIcon /> : <PlayIcon />}
          {playLabel}
        </button>
        <button
          type="button"
          onClick={restart}
          className="inline-flex items-center gap-2 rounded-full border border-kinora-line px-4 py-2.5 text-sm font-medium text-kinora-mist transition-colors hover:border-kinora-iris/60 hover:bg-white/5"
        >
          <RestartIcon />
          Restart
        </button>
        <p className="ml-auto text-xs tabular-nums text-kinora-muted" aria-live="off">
          Shot {currentBeat + 1}/{beats.length} · word {Math.min(focus + 1, total)}/{total}
        </p>
      </div>

      <div className="border-t border-kinora-line/80 px-5 pb-5 pt-4">
        <div className="flex gap-1.5" aria-hidden="true">
          {beats.map((beat) => (
            <div
              key={beat.id}
              className={`h-1.5 flex-1 rounded-full ${zoneBarClass(zoneForOffset(beat.id - currentBeat))}`}
            />
          ))}
        </div>
        <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-kinora-muted">
          <li className="inline-flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-kinora-glow" aria-hidden="true" />
            Committed · video ready
          </li>
          <li className="inline-flex items-center gap-2">
            <span className="h-2 w-2 rounded-full ring-1 ring-inset ring-kinora-iris/70" aria-hidden="true" />
            Speculative · keyframe only
          </li>
          <li className="inline-flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-kinora-line" aria-hidden="true" />
            Cold · plan + canon
          </li>
        </ul>
      </div>
    </div>
  );
}

export default ReaderPreview;
