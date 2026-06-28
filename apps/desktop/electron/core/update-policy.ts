/**
 * Auto-update *policy* — pure decisions, no `electron-updater`, no network.
 *
 * The Electron-bound `auto-update` service wraps `electron-updater` and feeds
 * its raw lifecycle events into the functions here, which decide:
 *   • whether THIS install is inside a staged-rollout cohort,
 *   • the next {@link UpdateStatus} to broadcast to the renderer,
 *   • whether enough time has elapsed to re-check.
 * Keeping the decisions pure means the rollout math and the status reducer are
 * unit-testable without launching Electron or hitting an update feed.
 */
import type { UpdateStatus, UpdatePhase } from "../shared/ipc-contract.js";

export type { UpdateStatus, UpdatePhase };

/**
 * Deterministically decide whether an install identified by `machineId` is in
 * the rollout cohort for a given `rolloutPercent` (0–100). Uses a stable hash
 * so the *same* machine always lands in the same bucket — a machine that's "in"
 * at 25% is still "in" at 50%, giving a true staged rollout rather than a
 * coin-flip each launch.
 */
export function isInRolloutCohort(machineId: string, rolloutPercent: number): boolean {
  const pct = clampPercent(rolloutPercent);
  if (pct >= 100) return true;
  if (pct <= 0) return false;
  const bucket = stableBucket(machineId); // 0–99
  return bucket < pct;
}

/** Stable 0–99 bucket from an arbitrary string (FNV-1a, no deps). */
export function stableBucket(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0) % 100;
}

export interface RolloutConfig {
  /** Master switch — disabled => updater never even checks. */
  enabled: boolean;
  /** 0–100. <100 means staged. */
  rolloutPercent: number;
  /** Update channel (`latest`, `beta`, …). */
  channel: string;
  /** Min ms between automatic checks. */
  checkIntervalMs: number;
  /** Require a valid code signature before installing (publisher pinning). */
  requireSignature: boolean;
}

export const DEFAULT_ROLLOUT: RolloutConfig = {
  enabled: true,
  rolloutPercent: 100,
  channel: "latest",
  checkIntervalMs: 6 * 60 * 60 * 1000, // 6h
  requireSignature: true,
};

/**
 * Should the service perform an automatic check now? Off when disabled, when
 * the machine is outside the cohort, or when we checked too recently.
 */
export function shouldAutoCheck(
  cfg: RolloutConfig,
  machineId: string,
  lastCheckMs: number | null,
  nowMs: number,
): boolean {
  if (!cfg.enabled) return false;
  if (!isInRolloutCohort(machineId, cfg.rolloutPercent)) return false;
  if (lastCheckMs != null && nowMs - lastCheckMs < cfg.checkIntervalMs) return false;
  return true;
}

/** Raw lifecycle events emitted by the updater service. */
export type UpdateEvent =
  | { type: "disabled" }
  | { type: "checking" }
  | { type: "available"; version: string }
  | { type: "not-available" }
  | { type: "progress"; percent: number; bytesPerSecond: number }
  | { type: "downloaded"; version: string }
  | { type: "error"; message: string };

/**
 * Reduce a lifecycle event into the next {@link UpdateStatus} to broadcast.
 * Pure: same (prev, event, staged) always yields the same status.
 */
export function reduceUpdateStatus(prev: UpdateStatus, event: UpdateEvent, staged: boolean): UpdateStatus {
  switch (event.type) {
    case "disabled":
      return { phase: "disabled", stagedRollout: staged };
    case "checking":
      return { phase: "checking", stagedRollout: staged };
    case "available":
      return { phase: "available", version: event.version, stagedRollout: staged };
    case "not-available":
      return { phase: "not-available", stagedRollout: staged };
    case "progress":
      return {
        phase: "downloading",
        version: prev.version,
        percent: Math.round(clampPercent(event.percent)),
        bytesPerSecond: Math.max(0, Math.round(event.bytesPerSecond)),
        stagedRollout: staged,
      };
    case "downloaded":
      return { phase: "downloaded", version: event.version, percent: 100, stagedRollout: staged };
    case "error":
      return { phase: "error", message: event.message, stagedRollout: staged };
  }
}

/** A status where quit-and-install is meaningful. */
export function canInstall(status: UpdateStatus): boolean {
  return status.phase === "downloaded";
}

function clampPercent(p: number): number {
  if (!Number.isFinite(p)) return 0;
  return Math.max(0, Math.min(100, p));
}
