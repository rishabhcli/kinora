// Usage meters (account domain) — pure math for the billing UI's consumption
// bars: how much of an entitlement (video-seconds, director edits, concurrent
// films) the reader has used this period. Handles the Infinity (unlimited)
// entitlements gracefully and formats friendly labels. No state, no network.
import type { Entitlements } from "./billing";

export interface Meter {
  /** Used amount this period. */
  used: number;
  /** The cap (Infinity = unlimited). */
  limit: number;
  /** 0..1 (0 when unlimited). */
  fraction: number;
  /** True once usage crosses the warn threshold (default 0.8). */
  nearLimit: boolean;
  /** True once used >= limit (never for unlimited). */
  exhausted: boolean;
  unlimited: boolean;
}

/** Build a meter from used + limit. Clamps fraction to [0,1]; unlimited caps
 *  read as fraction 0 + nearLimit/exhausted false. */
export function meter(used: number, limit: number, warnAt = 0.8): Meter {
  const u = Math.max(0, used);
  if (!Number.isFinite(limit)) {
    return { used: u, limit: Infinity, fraction: 0, nearLimit: false, exhausted: false, unlimited: true };
  }
  const safeLimit = Math.max(0, limit);
  const fraction = safeLimit === 0 ? 1 : Math.min(1, u / safeLimit);
  return {
    used: u,
    limit: safeLimit,
    fraction,
    nearLimit: fraction >= warnAt && fraction < 1,
    exhausted: u >= safeLimit,
    unlimited: false,
  };
}

export interface UsageSnapshot {
  videoSeconds: number;
  directorEdits: number;
  concurrentFilms: number;
}

export interface UsageMeters {
  videoSeconds: Meter;
  directorEdits: Meter;
  concurrentFilms: Meter;
}

/** Build all three meters from a usage snapshot + the plan's entitlements. */
export function usageMeters(usage: UsageSnapshot, ent: Entitlements): UsageMeters {
  return {
    videoSeconds: meter(usage.videoSeconds, ent.monthlyVideoSeconds),
    directorEdits: meter(usage.directorEdits, ent.directorEdits),
    concurrentFilms: meter(usage.concurrentFilms, ent.concurrentFilms),
  };
}

/** A friendly "12 / 60 min" or "12 min · unlimited" label for a seconds meter. */
export function formatSecondsMeter(m: Meter): string {
  const usedMin = Math.round(m.used / 60);
  if (m.unlimited) return `${usedMin} min used · unlimited`;
  const limitMin = Math.round(m.limit / 60);
  return `${usedMin} / ${limitMin} min`;
}

/** A friendly "n / m" or "n · unlimited" label for a count meter. */
export function formatCountMeter(m: Meter): string {
  if (m.unlimited) return `${m.used} · unlimited`;
  return `${m.used} / ${m.limit}`;
}

/** Whether any metered entitlement is at/near its cap — drives an upgrade nudge. */
export function shouldNudgeUpgrade(meters: UsageMeters): boolean {
  return Object.values(meters).some((m) => m.nearLimit || m.exhausted);
}
