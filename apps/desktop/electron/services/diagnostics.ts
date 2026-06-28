/**
 * Crash reporting + a diagnostics snapshot for the in-app diagnostics panel.
 *
 * `crashReporter.start` is wired with `uploadToServer: false` by default so we
 * never silently exfiltrate dumps — a real submit URL is parameterised via env
 * (`KINORA_CRASH_URL`) for builds that opt in. We also capture renderer/process
 * `render-process-gone` + `child-process-gone` and the main-process
 * `uncaughtException`/`unhandledRejection` into the structured log so the panel
 * can show recent failures.
 */
import { app, crashReporter } from "electron";
import os from "node:os";
import type { DiagnosticsSnapshot } from "../shared/ipc-contract.js";
import type { Logger, ScopedLogger } from "../core/logger.js";

export interface DiagnosticsDeps {
  logger: Logger;
  log: ScopedLogger;
  apiBaseUrl: string;
  liveVideo: boolean;
  windowCount: () => number;
  /** Optional crash upload endpoint; absent => store-locally only. */
  submitUrl?: string;
}

export class Diagnostics {
  private readonly deps: DiagnosticsDeps;

  constructor(deps: DiagnosticsDeps) {
    this.deps = deps;
  }

  start(): void {
    try {
      crashReporter.start({
        productName: "Kinora",
        companyName: "Kinora",
        submitURL: this.deps.submitUrl ?? "",
        uploadToServer: Boolean(this.deps.submitUrl),
        compress: true,
        ignoreSystemCrashHandler: false,
      });
      this.deps.log.info("crash reporter started", { upload: Boolean(this.deps.submitUrl) });
    } catch (err) {
      this.deps.log.warn("crash reporter unavailable", { message: msg(err) });
    }

    process.on("uncaughtException", (err) => {
      this.deps.log.error("uncaughtException", { message: err.message, stack: err.stack });
    });
    process.on("unhandledRejection", (reason) => {
      this.deps.log.error("unhandledRejection", { message: msg(reason) });
    });

    app.on("render-process-gone", (_e, _wc, details) => {
      this.deps.log.error("render-process-gone", { reason: details.reason, exitCode: details.exitCode });
    });
    app.on("child-process-gone", (_e, details) => {
      this.deps.log.error("child-process-gone", { type: details.type, reason: details.reason });
    });
  }

  snapshot(): DiagnosticsSnapshot {
    const mem = process.memoryUsage();
    return {
      appName: app.getName(),
      appVersion: app.getVersion(),
      electron: process.versions.electron ?? "",
      chrome: process.versions.chrome ?? "",
      node: process.versions.node ?? "",
      v8: process.versions.v8 ?? "",
      platform: process.platform,
      arch: process.arch,
      osRelease: safe(() => os.release(), ""),
      locale: safe(() => app.getLocale(), ""),
      uptimeSec: Math.round(process.uptime()),
      memory: {
        rssMB: round(mem.rss / 1e6),
        heapUsedMB: round(mem.heapUsed / 1e6),
        heapTotalMB: round(mem.heapTotal / 1e6),
      },
      windows: this.deps.windowCount(),
      packaged: app.isPackaged,
      liveVideo: this.deps.liveVideo,
      apiBaseUrl: this.deps.apiBaseUrl,
      logCount: this.deps.logger.count(),
    };
  }
}

function round(n: number): number {
  return Math.round(n * 10) / 10;
}
function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback;
  }
}
function msg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
