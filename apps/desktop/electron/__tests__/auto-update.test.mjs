import test from "node:test";
import assert from "node:assert/strict";
import { AutoUpdateService } from "../../dist-electron/services/auto-update.js";

const silentLog = { debug() {}, info() {}, warn() {}, error() {} };

/** A fake electron-updater autoUpdater driven by emit(). */
function fakeUpdater() {
  const listeners = new Map();
  return {
    autoDownload: false,
    autoInstallOnAppQuit: false,
    allowDowngrade: true,
    channel: null,
    installed: false,
    checks: 0,
    on(event, cb) {
      const arr = listeners.get(event) ?? [];
      arr.push(cb);
      listeners.set(event, arr);
      return this;
    },
    emit(event, ...args) {
      for (const cb of listeners.get(event) ?? []) cb(...args);
    },
    async checkForUpdates() {
      this.checks++;
      return {};
    },
    quitAndInstall() {
      this.installed = true;
    },
  };
}

test("disabled when not packaged", () => {
  const statuses = [];
  const svc = new AutoUpdateService({
    log: silentLog,
    onStatus: (s) => statuses.push(s),
    updater: fakeUpdater(),
    isPackaged: false,
  });
  svc.start();
  assert.equal(svc.current.phase, "disabled");
});

test("disabled when no updater module is present", () => {
  const svc = new AutoUpdateService({
    log: silentLog,
    onStatus: () => {},
    updater: null,
    isPackaged: true,
  });
  svc.start();
  assert.equal(svc.current.phase, "disabled");
});

test("wires updater events into broadcast statuses", () => {
  const statuses = [];
  const u = fakeUpdater();
  const svc = new AutoUpdateService({
    log: silentLog,
    onStatus: (s) => statuses.push(s),
    updater: u,
    isPackaged: true,
    config: { rolloutPercent: 100, checkIntervalMs: 10_000 },
    now: () => 0,
  });
  svc.start();
  u.emit("checking-for-update");
  u.emit("update-available", { version: "2.0.0" });
  u.emit("download-progress", { percent: 50, bytesPerSecond: 2048 });
  u.emit("update-downloaded", { version: "2.0.0" });
  const phases = statuses.map((s) => s.phase);
  assert.ok(phases.includes("checking"));
  assert.ok(phases.includes("available"));
  assert.ok(phases.includes("downloading"));
  assert.equal(svc.current.phase, "downloaded");
  assert.equal(svc.current.version, "2.0.0");
  // autoDownload should be turned on by start().
  assert.equal(u.autoDownload, true);
  svc.stop();
});

test("a machine outside the rollout cohort never auto-checks", () => {
  const u = fakeUpdater();
  const svc = new AutoUpdateService({
    log: silentLog,
    onStatus: () => {},
    updater: u,
    isPackaged: true,
    machineId: "definitely-out",
    config: { rolloutPercent: 0, checkIntervalMs: 1 },
    now: () => 1000,
  });
  svc.start();
  assert.equal(u.checks, 0); // gated out by cohort
  svc.stop();
});

test("checkNow forces a check regardless of interval", async () => {
  const u = fakeUpdater();
  const svc = new AutoUpdateService({
    log: silentLog,
    onStatus: () => {},
    updater: u,
    isPackaged: true,
    config: { rolloutPercent: 100 },
    now: () => 0,
  });
  await svc.checkNow();
  assert.equal(u.checks, 1);
  svc.stop();
});

test("installNow only installs when an update is downloaded", () => {
  const u = fakeUpdater();
  const svc = new AutoUpdateService({ log: silentLog, onStatus: () => {}, updater: u, isPackaged: true });
  svc.start();
  assert.equal(svc.installNow(), false); // nothing downloaded yet
  u.emit("update-downloaded", { version: "3.0.0" });
  assert.equal(svc.installNow(), true);
  assert.equal(u.installed, true);
  svc.stop();
});

test("staged flag reflects rollout < 100", () => {
  const staged = new AutoUpdateService({
    log: silentLog,
    onStatus: () => {},
    updater: fakeUpdater(),
    isPackaged: true,
    config: { rolloutPercent: 30 },
  });
  assert.equal(staged.isStaged, true);
  const full = new AutoUpdateService({
    log: silentLog,
    onStatus: () => {},
    updater: fakeUpdater(),
    isPackaged: true,
    config: { rolloutPercent: 100 },
  });
  assert.equal(full.isStaged, false);
});
