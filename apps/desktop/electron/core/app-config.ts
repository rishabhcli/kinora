/**
 * Reads main-process configuration from the environment into a typed object.
 * Pure (takes an env map) so the parsing/defaulting is unit-testable. The
 * renderer's own config (VITE_*) is separate; these are the knobs the *shell*
 * cares about (update rollout, crash submit URL, log level, API base for the
 * diagnostics panel).
 */
import type { LogLevel } from "./logger.js";
import type { RolloutConfig } from "./update-policy.js";
import { DEFAULT_ROLLOUT } from "./update-policy.js";

export interface AppConfig {
  apiBaseUrl: string;
  liveVideo: boolean;
  logLevel: LogLevel;
  crashSubmitUrl?: string;
  rollout: RolloutConfig;
  enableTray: boolean;
}

export function readAppConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const debug = isTrue(env.KINORA_DEBUG);
  return {
    apiBaseUrl: env.VITE_KINORA_API_URL || env.KINORA_API_URL || "http://localhost:8000",
    liveVideo: isTrue(env.KINORA_LIVE_VIDEO),
    logLevel: parseLevel(env.KINORA_LOG_LEVEL) ?? (debug ? "debug" : "info"),
    crashSubmitUrl: nonEmpty(env.KINORA_CRASH_URL),
    enableTray: !isFalse(env.KINORA_TRAY),
    rollout: {
      enabled: !isFalse(env.KINORA_UPDATE_ENABLED),
      rolloutPercent: parsePercent(env.KINORA_UPDATE_ROLLOUT, DEFAULT_ROLLOUT.rolloutPercent),
      channel: nonEmpty(env.KINORA_UPDATE_CHANNEL) ?? DEFAULT_ROLLOUT.channel,
      checkIntervalMs: parseMs(env.KINORA_UPDATE_INTERVAL_MS, DEFAULT_ROLLOUT.checkIntervalMs),
      requireSignature: !isFalse(env.KINORA_UPDATE_REQUIRE_SIGNATURE),
    },
  };
}

function isTrue(v: string | undefined): boolean {
  return /^(1|true|yes|on)$/i.test((v ?? "").trim());
}
function isFalse(v: string | undefined): boolean {
  return /^(0|false|no|off)$/i.test((v ?? "").trim());
}
function nonEmpty(v: string | undefined): string | undefined {
  const t = (v ?? "").trim();
  return t.length > 0 ? t : undefined;
}
function parseLevel(v: string | undefined): LogLevel | null {
  const t = (v ?? "").trim().toLowerCase();
  return t === "debug" || t === "info" || t === "warn" || t === "error" ? t : null;
}
function parsePercent(v: string | undefined, fallback: number): number {
  const n = Number(v);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(100, n));
}
function parseMs(v: string | undefined, fallback: number): number {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}
