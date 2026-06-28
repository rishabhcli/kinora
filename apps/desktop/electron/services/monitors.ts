/**
 * Power + network monitoring. Watches `powerMonitor` and the main-process
 * `net.isOnline()` and emits a coalesced {@link SystemState} whenever anything
 * changes, so the renderer can pause speculative work on battery / show an
 * offline banner. The state-diff logic is pure (see {@link sameSystemState}).
 */
import { powerMonitor } from "electron";
import type { SystemState } from "../shared/ipc-contract.js";
import type { ScopedLogger } from "../core/logger.js";
import { normalizeThermal, sameSystemState } from "../core/system-state.js";

export { sameSystemState };

export interface MonitorsDeps {
  log: ScopedLogger;
  onChange: (state: SystemState) => void;
  /** Injectable online probe (defaults to Electron `net.isOnline`). */
  isOnline?: () => boolean;
  /** Poll interval for the online probe (network has no reliable event). */
  pollMs?: number;
}

export class SystemMonitors {
  private readonly deps: MonitorsDeps;
  private state: SystemState;
  private timer: NodeJS.Timeout | null = null;
  private readonly isOnline: () => boolean;

  constructor(deps: MonitorsDeps) {
    this.deps = deps;
    this.isOnline = deps.isOnline ?? defaultIsOnline;
    this.state = this.read();
  }

  current(): SystemState {
    return this.state;
  }

  start(): void {
    const onPower = () => this.refresh();
    powerMonitor.on("on-battery", onPower);
    powerMonitor.on("on-ac", onPower);
    powerMonitor.on("suspend", () => this.patch({ suspended: true }));
    powerMonitor.on("resume", () => this.patch({ suspended: false }));
    if ("on" in powerMonitor) {
      try {
        // Available on macOS/Windows; guarded so Linux doesn't throw.
        powerMonitor.on("thermal-state-change" as never, onPower);
      } catch {
        /* not all platforms emit this */
      }
    }
    const pollMs = this.deps.pollMs ?? 5000;
    this.timer = setInterval(() => this.refresh(), pollMs);
    if (typeof this.timer.unref === "function") this.timer.unref();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    powerMonitor.removeAllListeners();
  }

  private patch(partial: Partial<SystemState>): void {
    this.emitIfChanged({ ...this.state, ...partial });
  }

  private refresh(): void {
    this.emitIfChanged(this.read());
  }

  private emitIfChanged(next: SystemState): void {
    if (sameSystemState(this.state, next)) return;
    this.state = next;
    this.deps.log.info("system state changed", { ...next });
    this.deps.onChange(next);
  }

  private read(): SystemState {
    return {
      online: safe(() => this.isOnline(), true),
      onBattery: safe(() => powerMonitor.isOnBatteryPower(), false),
      thermalState: safe(() => normalizeThermal(powerMonitor.getCurrentThermalState?.()), "unknown"),
      suspended: this.state?.suspended ?? false,
    };
  }
}

function defaultIsOnline(): boolean {
  try {
    // `net.isOnline` is sync and cheap; required lazily to avoid pulling net
    // into the pure import graph during tests.
    const { net } = require("electron") as typeof import("electron");
    return net.isOnline();
  } catch {
    return true;
  }
}

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback;
  }
}
