/**
 * System-state value helpers — pure, Electron-free. The monitors service uses
 * these to coalesce power/network changes; kept separate so the equality +
 * normalisation logic is unit-testable.
 */
import type { SystemState } from "../shared/ipc-contract.js";

export function sameSystemState(a: SystemState, b: SystemState): boolean {
  return (
    a.online === b.online &&
    a.onBattery === b.onBattery &&
    a.thermalState === b.thermalState &&
    a.suspended === b.suspended
  );
}

export function normalizeThermal(value: unknown): SystemState["thermalState"] {
  switch (value) {
    case "nominal":
    case "fair":
    case "serious":
    case "critical":
      return value;
    default:
      return "unknown";
  }
}
