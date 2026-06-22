import type { CSSProperties } from "react";

// Client-side Ken-Burns bridge (kinora.md §4.4 / §4.8). On a seek we show the
// new position's keyframe under a slow zoom/drift *immediately* — zero
// generation cost — so the reader sees something coherent within one frame
// while real video renders. Origin/duration are derived deterministically from
// a seed so a given shot always pans the same way (no reshuffle on re-render).

const ORIGINS = [
  "32% 30%",
  "68% 34%",
  "40% 62%",
  "60% 56%",
  "50% 38%",
  "36% 50%",
];

export function hashSeed(input: string): number {
  let h = 2166136261;
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export interface KenBurnsOptions {
  /** Pan duration in seconds (default 16–22s, varied by seed). */
  durationS?: number;
}

/** CSS custom properties consumed by the `.ken-burns-bridge` utility class. */
export function kenBurnsStyle(
  seed: string | number,
  opts: KenBurnsOptions = {},
): CSSProperties {
  const n = typeof seed === "number" ? Math.abs(Math.trunc(seed)) : hashSeed(seed);
  const origin = ORIGINS[n % ORIGINS.length];
  const duration = opts.durationS ?? 16 + (n % 7);
  const vars: Record<string, string> = {
    "--kb-origin": origin,
    "--kb-duration": `${duration}s`,
  };
  return vars as unknown as CSSProperties;
}
