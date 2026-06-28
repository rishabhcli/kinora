import test from "node:test";
import assert from "node:assert/strict";
import { readAppConfig } from "../../dist-electron/core/app-config.js";

test("defaults with an empty env", () => {
  const c = readAppConfig({});
  assert.equal(c.apiBaseUrl, "http://localhost:8000");
  assert.equal(c.liveVideo, false);
  assert.equal(c.logLevel, "info");
  assert.equal(c.crashSubmitUrl, undefined);
  assert.equal(c.enableTray, true);
  assert.equal(c.rollout.enabled, true);
  assert.equal(c.rollout.rolloutPercent, 100);
  assert.equal(c.rollout.channel, "latest");
  assert.equal(c.rollout.requireSignature, true);
});

test("VITE_KINORA_API_URL wins, falls back to KINORA_API_URL", () => {
  assert.equal(readAppConfig({ VITE_KINORA_API_URL: "https://a" }).apiBaseUrl, "https://a");
  assert.equal(readAppConfig({ KINORA_API_URL: "https://b" }).apiBaseUrl, "https://b");
});

test("KINORA_DEBUG bumps default log level to debug", () => {
  assert.equal(readAppConfig({ KINORA_DEBUG: "1" }).logLevel, "debug");
  assert.equal(readAppConfig({ KINORA_LOG_LEVEL: "warn", KINORA_DEBUG: "1" }).logLevel, "warn");
});

test("boolean env parsing is forgiving", () => {
  assert.equal(readAppConfig({ KINORA_LIVE_VIDEO: "true" }).liveVideo, true);
  assert.equal(readAppConfig({ KINORA_LIVE_VIDEO: "YES" }).liveVideo, true);
  assert.equal(readAppConfig({ KINORA_LIVE_VIDEO: "off" }).liveVideo, false);
  assert.equal(readAppConfig({ KINORA_TRAY: "false" }).enableTray, false);
  assert.equal(readAppConfig({ KINORA_UPDATE_ENABLED: "0" }).rollout.enabled, false);
});

test("rollout percent + interval are clamped/validated", () => {
  assert.equal(readAppConfig({ KINORA_UPDATE_ROLLOUT: "25" }).rollout.rolloutPercent, 25);
  assert.equal(readAppConfig({ KINORA_UPDATE_ROLLOUT: "999" }).rollout.rolloutPercent, 100);
  assert.equal(readAppConfig({ KINORA_UPDATE_ROLLOUT: "-5" }).rollout.rolloutPercent, 0);
  assert.equal(readAppConfig({ KINORA_UPDATE_ROLLOUT: "x" }).rollout.rolloutPercent, 100);
  assert.equal(readAppConfig({ KINORA_UPDATE_INTERVAL_MS: "1000" }).rollout.checkIntervalMs, 1000);
  assert.ok(readAppConfig({ KINORA_UPDATE_INTERVAL_MS: "-1" }).rollout.checkIntervalMs > 0);
});

test("crash submit url passes through when set", () => {
  assert.equal(readAppConfig({ KINORA_CRASH_URL: "https://crash" }).crashSubmitUrl, "https://crash");
  assert.equal(readAppConfig({ KINORA_CRASH_URL: "  " }).crashSubmitUrl, undefined);
});

test("update channel + signature toggles", () => {
  assert.equal(readAppConfig({ KINORA_UPDATE_CHANNEL: "beta" }).rollout.channel, "beta");
  assert.equal(readAppConfig({ KINORA_UPDATE_REQUIRE_SIGNATURE: "false" }).rollout.requireSignature, false);
});
