/**
 * Auto-update via `electron-updater`, with a staged-rollout gate and signature
 * enforcement. The rollout math + status reducer are the pure `update-policy`;
 * this service just adapts the updater's events into them and broadcasts.
 *
 * `electron-updater` is an OPTIONAL runtime dependency: it is `require`d lazily
 * inside a try/catch, so a build that hasn't installed it (or a dev run, or an
 * unpackaged app) degrades to `phase: "disabled"` instead of crashing. This is
 * also why we type the updater structurally rather than importing its types.
 */
import {
  DEFAULT_ROLLOUT,
  canInstall,
  reduceUpdateStatus,
  shouldAutoCheck,
  type RolloutConfig,
  type UpdateEvent,
  type UpdateStatus,
} from "../core/update-policy.js";
import type { ScopedLogger } from "../core/logger.js";

/** The slice of `electron-updater`'s autoUpdater we actually use. */
interface AutoUpdaterLike {
  autoDownload: boolean;
  autoInstallOnAppQuit: boolean;
  allowDowngrade: boolean;
  channel: string | null;
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  checkForUpdates(): Promise<unknown>;
  quitAndInstall(isSilent?: boolean, isForceRunAfter?: boolean): void;
}

export interface AutoUpdateDeps {
  log: ScopedLogger;
  config?: Partial<RolloutConfig>;
  /** Stable per-install id for cohort math (defaults to a derived id). */
  machineId?: string;
  onStatus: (status: UpdateStatus) => void;
  /** Injectable updater for tests; defaults to lazy `electron-updater`. */
  updater?: AutoUpdaterLike | null;
  now?: () => number;
  /**
   * Whether the app is packaged. Updates only run in packaged builds; in dev we
   * stay `disabled`. Defaults to a lazy `app.isPackaged` read (false off-Electron).
   */
  isPackaged?: boolean;
}

export class AutoUpdateService {
  private readonly log: ScopedLogger;
  private readonly cfg: RolloutConfig;
  private readonly machineId: string;
  private readonly onStatus: (s: UpdateStatus) => void;
  private readonly now: () => number;
  private updater: AutoUpdaterLike | null;
  private readonly packaged: boolean;
  private status: UpdateStatus = { phase: "idle" };
  private lastCheck: number | null = null;
  private timer: NodeJS.Timeout | null = null;

  constructor(deps: AutoUpdateDeps) {
    this.log = deps.log;
    this.cfg = { ...DEFAULT_ROLLOUT, ...deps.config };
    this.machineId = deps.machineId ?? deriveMachineId();
    this.onStatus = deps.onStatus;
    this.now = deps.now ?? Date.now;
    this.updater = deps.updater !== undefined ? deps.updater : loadUpdater(this.log);
    this.packaged = deps.isPackaged ?? isAppPackaged();
  }

  get current(): UpdateStatus {
    return this.status;
  }

  get isStaged(): boolean {
    return this.cfg.rolloutPercent < 100;
  }

  /** Wire updater events and schedule periodic checks. Safe to call once. */
  start(): void {
    if (!this.cfg.enabled || !this.updater || !this.packaged) {
      this.emit({ type: "disabled" });
      this.log.info("auto-update disabled", {
        enabled: this.cfg.enabled,
        hasUpdater: Boolean(this.updater),
        packaged: this.packaged,
      });
      return;
    }

    const u = this.updater;
    u.autoDownload = true;
    u.autoInstallOnAppQuit = true;
    u.allowDowngrade = false;
    if (this.cfg.channel) u.channel = this.cfg.channel;

    u.on("checking-for-update", () => this.emit({ type: "checking" }));
    u.on("update-available", (info: unknown) =>
      this.emit({ type: "available", version: versionOf(info) }),
    );
    u.on("update-not-available", () => this.emit({ type: "not-available" }));
    u.on("download-progress", (p: unknown) => {
      const prog = p as { percent?: number; bytesPerSecond?: number };
      this.emit({
        type: "progress",
        percent: prog.percent ?? 0,
        bytesPerSecond: prog.bytesPerSecond ?? 0,
      });
    });
    u.on("update-downloaded", (info: unknown) =>
      this.emit({ type: "downloaded", version: versionOf(info) }),
    );
    u.on("error", (err: unknown) => this.emit({ type: "error", message: msg(err) }));

    // Initial + periodic checks, both gated by the rollout cohort.
    void this.maybeCheck();
    this.timer = setInterval(() => void this.maybeCheck(), Math.max(60_000, this.cfg.checkIntervalMs));
    if (typeof this.timer.unref === "function") this.timer.unref();
  }

  /** Force a check now (from the menu / IPC), bypassing the interval gate. */
  async checkNow(): Promise<UpdateStatus> {
    if (!this.updater || !this.cfg.enabled) {
      this.emit({ type: "disabled" });
      return this.status;
    }
    this.lastCheck = this.now();
    try {
      await this.updater.checkForUpdates();
    } catch (err) {
      this.emit({ type: "error", message: msg(err) });
    }
    return this.status;
  }

  /** Quit and install a downloaded update (no-op unless one is ready). */
  installNow(): boolean {
    if (!this.updater || !canInstall(this.status)) return false;
    try {
      this.updater.quitAndInstall(false, true);
      return true;
    } catch (err) {
      this.log.error("auto-update: install failed", { message: msg(err) });
      return false;
    }
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private async maybeCheck(): Promise<void> {
    if (!shouldAutoCheck(this.cfg, this.machineId, this.lastCheck, this.now())) {
      this.log.debug("auto-update: skipping check (cohort/interval)");
      return;
    }
    await this.checkNow();
  }

  private emit(event: UpdateEvent): void {
    this.status = reduceUpdateStatus(this.status, event, this.isStaged);
    this.onStatus(this.status);
  }
}

function loadUpdater(log: ScopedLogger): AutoUpdaterLike | null {
  try {
    // Optional dependency — present only in packaged builds that bundle it.
    const mod = require("electron-updater") as { autoUpdater?: AutoUpdaterLike };
    return mod.autoUpdater ?? null;
  } catch {
    log.info("auto-update: electron-updater not installed; updates disabled");
    return null;
  }
}

function deriveMachineId(): string {
  try {
    const { app } = require("electron") as typeof import("electron");
    return `${app.getName()}:${app.getVersion()}:${process.platform}:${process.arch}`;
  } catch {
    return "kinora:dev";
  }
}

function isAppPackaged(): boolean {
  try {
    const { app } = require("electron") as typeof import("electron");
    return Boolean(app.isPackaged);
  } catch {
    return false;
  }
}

function versionOf(info: unknown): string {
  if (info && typeof info === "object" && "version" in info) {
    const v = (info as { version?: unknown }).version;
    if (typeof v === "string") return v;
  }
  return "";
}
function msg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
