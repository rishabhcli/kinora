/**
 * perf.ts — Agent 07's opt-in client performance helpers.
 *
 * Importing this module changes nothing; a component opts in. Published in
 * coordination/CONTRACTS.md for Agents 2/4/8/10 to adopt.
 *
 * - `lazyImport` — `React.lazy` + a one-shot retry on a chunk-load error (a
 *   transient blip or a stale chunk after a redeploy) so a flaky dynamic import
 *   never blanks the screen.
 * - `preloadVideo` — warm the HTTP cache for an upcoming clip (low-priority
 *   `<link rel="prefetch">`), idempotent per URL, so the next crossfade starts
 *   instantly without competing with the clip that is playing now.
 * - `decodeOnIdle` — decode an image off the critical path (idle-gated
 *   `img.decode()`) so first paint of a shelf/keyframe doesn't jank.
 * - `mark` / `measure` — thin Performance API wrappers for TTI / decode marks,
 *   no-ops where the API is unavailable.
 */

import { lazy, type ComponentType, type LazyExoticComponent } from "react";

/** `React.lazy` with a single retry, so a transient chunk-load failure self-heals. */
export function lazyImport<T extends ComponentType<unknown>>(
  factory: () => Promise<{ default: T }>,
): LazyExoticComponent<T> {
  return lazy(() => factory().catch(() => factory()));
}

const preloadedVideos = new Set<string>();

/** Prefetch an upcoming clip into the HTTP cache (low priority, idempotent per URL). */
export function preloadVideo(url: string): void {
  if (!url || preloadedVideos.has(url) || typeof document === "undefined") return;
  preloadedVideos.add(url);
  const link = document.createElement("link");
  link.rel = "prefetch";
  link.as = "video";
  link.href = url;
  document.head.appendChild(link);
}

interface IdleWindow {
  requestIdleCallback?: (cb: () => void) => number;
}

/** Decode an image off the critical path; resolves even when decode/idle are unavailable. */
export function decodeOnIdle(img: HTMLImageElement): Promise<void> {
  return new Promise<void>((resolve) => {
    const run = (): void => {
      if (typeof img.decode === "function") {
        img.decode().then(
          () => resolve(),
          () => resolve(),
        );
      } else {
        resolve();
      }
    };
    const ric = (globalThis as unknown as IdleWindow).requestIdleCallback;
    if (typeof ric === "function") ric(run);
    else setTimeout(run, 0);
  });
}

/** Record a Performance API mark (no-op where unavailable). */
export function mark(name: string): void {
  if (typeof performance !== "undefined" && typeof performance.mark === "function") {
    performance.mark(name);
  }
}

/** Measure ms between a start mark and now; `undefined` where unavailable. */
export function measure(name: string, startMark: string): number | undefined {
  if (typeof performance === "undefined" || typeof performance.measure !== "function") {
    return undefined;
  }
  try {
    const entry = performance.measure(name, startMark);
    return entry?.duration;
  } catch {
    return undefined;
  }
}
