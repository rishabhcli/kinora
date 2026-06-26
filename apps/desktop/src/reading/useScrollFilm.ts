import { useEffect, useRef, type RefObject } from "react";
import {
  computeFrame,
  schedulerSignal,
  scrollVelocity,
  type SchedulerSignal,
  type Timeline,
} from "./timeline";
import type { FilmPaneHandle } from "./FilmPane";

export interface ScrollFrame {
  fraction: number;
  focusWord: number;
  mode: "scrub" | "play";
}

export interface UseScrollFilmArgs {
  scrollRef: RefObject<HTMLElement | null>;
  filmRef: RefObject<FilmPaneHandle | null>;
  timeline: Timeline;
  reducedMotion: boolean;
  /** signal the scheduler (live sessions only) — throttled, mirrors ReadingRoom */
  onSchedulerSignal?: (sig: SchedulerSignal) => void;
  /** throttled scroll fraction + focus word — for the shell (persistence/chrome) */
  onProgress?: (fraction: number, focusWord: number) => void;
  /** every frame, for imperative DOM side-effects only (parallax / rail / indicator) */
  onFrame?: (frame: ScrollFrame) => void;
}

const IDLE_MS = 220; // keep ticking this long after the last scroll, then settle to play
const SCHED_THROTTLE_MS = 150; // scheduler signalling cadence (≈ ReadingRoom's 160ms)
const VELOCITY_ALPHA = 0.35; // EMA smoothing for scrub/play decision (less flicker)

/** Binds a scroll container to the FilmPane: scroll position scrubs one continuous
 *  film. Runs a single rAF loop while scrolling (and self-stops when idle, after a
 *  final play-mode settle), writing `currentTime`/play state imperatively — no
 *  React state in the hot path. Preserves ReadingRoom's scheduler signalling. */
export function useScrollFilm({
  scrollRef,
  filmRef,
  timeline,
  reducedMotion,
  onSchedulerSignal,
  onProgress,
  onFrame,
}: UseScrollFilmArgs): void {
  // Latest config in a ref so the rAF loop (set up once) never goes stale and we
  // don't re-subscribe the scroll listener on every render.
  const cfg = useRef({ timeline, reducedMotion, onSchedulerSignal, onProgress, onFrame });
  cfg.current = { timeline, reducedMotion, onSchedulerSignal, onProgress, onFrame };

  const raf = useRef(0);
  const running = useRef(false);
  const startRef = useRef<() => void>(() => {});
  const lastScrollAt = useRef(0);
  const prevFocus = useRef(0);
  const prevAt = useRef(0);
  const emaVel = useRef(0);
  // scheduler-signal throttle state (separate cadence from the frame loop)
  const schedAt = useRef(0);
  const schedWord = useRef(0);
  const progressAt = useRef(0);

  useEffect(() => {
    const tick = () => {
      const el = scrollRef.current;
      if (!el) {
        running.current = false;
        return;
      }
      const now = performance.now();
      const range = Math.max(0, el.scrollHeight - el.clientHeight);
      const scrollTop = el.scrollTop;
      const fraction = range > 0 ? Math.min(1, Math.max(0, scrollTop / range)) : 0;

      // Velocity in words/sec from focus-word delta, EMA-smoothed.
      const totalWords = cfg.current.timeline.totalWords;
      const focusNow = Math.round(fraction * totalWords);
      const dt = prevAt.current ? (now - prevAt.current) / 1000 : 0;
      const instVel = scrollVelocity(prevFocus.current, focusNow, dt);
      emaVel.current = emaVel.current + VELOCITY_ALPHA * (instVel - emaVel.current);
      prevAt.current = now;
      prevFocus.current = focusNow;

      const liveDuration = filmRef.current?.getActiveDuration();
      const frame = computeFrame({
        timeline: cfg.current.timeline,
        scrollTop,
        scrollRange: range,
        velocityWordsPerSec: emaVel.current,
        liveDuration,
      });

      filmRef.current?.setPlayhead(frame.src, frame.time, frame.mode === "scrub");
      cfg.current.onFrame?.({ fraction: frame.fraction, focusWord: frame.focusWord, mode: frame.mode });

      // Throttled scheduler signalling (live sessions) + progress callback.
      if (now - schedAt.current >= SCHED_THROTTLE_MS) {
        const sdt = schedAt.current ? (now - schedAt.current) / 1000 : 0;
        if (cfg.current.onSchedulerSignal && schedAt.current > 0) {
          cfg.current.onSchedulerSignal(schedulerSignal(schedWord.current, frame.focusWord, sdt));
        }
        schedAt.current = now;
        schedWord.current = frame.focusWord;
      }
      if (now - progressAt.current >= SCHED_THROTTLE_MS) {
        progressAt.current = now;
        cfg.current.onProgress?.(frame.fraction, frame.focusWord);
      }

      // Keep ticking while recently scrolled or actively scrubbing; otherwise this
      // frame already applied play mode — stop and wait for the next scroll.
      if (now - lastScrollAt.current < IDLE_MS || frame.mode === "scrub") {
        raf.current = requestAnimationFrame(tick);
      } else {
        running.current = false;
      }
    };

    const start = () => {
      lastScrollAt.current = performance.now();
      if (running.current) return;
      running.current = true;
      raf.current = requestAnimationFrame(tick);
    };
    startRef.current = start;

    const onScroll = () => start();

    const el = scrollRef.current;
    el?.addEventListener("scroll", onScroll, { passive: true });
    start(); // initial paint: settle the playhead onto the current scroll position

    return () => {
      el?.removeEventListener("scroll", onScroll);
      cancelAnimationFrame(raf.current);
      running.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollRef, filmRef]);

  // When the film set changes (shots/clips load, or reduced-motion toggles), kick a
  // frame so the pane reflects the new timeline even without a scroll event.
  useEffect(() => {
    startRef.current();
  }, [timeline, reducedMotion]);
}
