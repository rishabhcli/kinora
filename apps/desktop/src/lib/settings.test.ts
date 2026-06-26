import { test } from "node:test";
import assert from "node:assert/strict";
import {
  SETTINGS_DEFAULTS,
  mergeSettings,
  resolveAppearance,
  diffFromDefaults,
  createSettingsStore,
  type AppSettings,
  type KeyValueStore,
} from "./settings.ts";

// A tiny in-memory KeyValueStore so the store is testable with no DOM.
const memStore = (seed?: Record<string, string>): KeyValueStore => {
  const m = new Map<string, string>(Object.entries(seed ?? {}));
  return {
    getItem: (k) => (m.has(k) ? m.get(k)! : null),
    setItem: (k, v) => void m.set(k, v),
  };
};

test("mergeSettings: empty/garbage → full defaults", () => {
  assert.deepEqual(mergeSettings(undefined), SETTINGS_DEFAULTS);
  assert.deepEqual(mergeSettings(null), SETTINGS_DEFAULTS);
  assert.deepEqual(mergeSettings("not an object"), SETTINGS_DEFAULTS);
  assert.deepEqual(mergeSettings(42), SETTINGS_DEFAULTS);
});

test("mergeSettings: keeps valid overrides, ignores unknown keys", () => {
  const merged = mergeSettings({ analytics: true, bogusKey: 99 } as Record<string, unknown>);
  assert.equal(merged.analytics, true);
  assert.equal(merged.weeklyDigest, SETTINGS_DEFAULTS.weeklyDigest);
  assert.ok(!("bogusKey" in merged));
});

test("mergeSettings: clamps scrubSensitivity into [0.5, 2] and rejects non-numbers", () => {
  assert.equal(mergeSettings({ scrubSensitivity: 5 }).scrubSensitivity, 2);
  assert.equal(mergeSettings({ scrubSensitivity: 0.1 }).scrubSensitivity, 0.5);
  assert.equal(mergeSettings({ scrubSensitivity: 1.25 }).scrubSensitivity, 1.25);
  assert.equal(
    mergeSettings({ scrubSensitivity: "fast" } as unknown as Partial<AppSettings>).scrubSensitivity,
    SETTINGS_DEFAULTS.scrubSensitivity,
  );
});

test("mergeSettings: enum-ish fields fall back to default when invalid", () => {
  assert.equal(mergeSettings({ reduceMotion: "maybe" } as never).reduceMotion, SETTINGS_DEFAULTS.reduceMotion);
  assert.equal(mergeSettings({ reduceMotion: "on" }).reduceMotion, "on");
  assert.equal(mergeSettings({ launchView: "Mars" } as never).launchView, SETTINGS_DEFAULTS.launchView);
});

test("resolveAppearance: on/off override the system, system follows it", () => {
  assert.equal(resolveAppearance("on", false), true);
  assert.equal(resolveAppearance("off", true), false);
  assert.equal(resolveAppearance("system", true), true);
  assert.equal(resolveAppearance("system", false), false);
});

test("diffFromDefaults: only changed keys are reported", () => {
  const d = diffFromDefaults({ ...SETTINGS_DEFAULTS, analytics: !SETTINGS_DEFAULTS.analytics });
  assert.deepEqual(Object.keys(d), ["analytics"]);
  assert.deepEqual(diffFromDefaults({ ...SETTINGS_DEFAULTS }), {});
});

test("store: get is defaults, set persists + notifies, reset restores", () => {
  const backing = memStore();
  const store = createSettingsStore(backing);
  assert.deepEqual(store.get(), SETTINGS_DEFAULTS);

  let notified = 0;
  const off = store.subscribe(() => notified++);

  store.set({ analytics: true });
  assert.equal(store.get().analytics, true);
  assert.equal(notified, 1);
  // persisted as JSON
  assert.equal(JSON.parse(backing.getItem("kinora.settings")!).analytics, true);

  // a fresh store reading the same backing rehydrates the change
  assert.equal(createSettingsStore(backing).get().analytics, true);

  store.reset();
  assert.deepEqual(store.get(), SETTINGS_DEFAULTS);
  assert.equal(notified, 2);

  off();
  store.set({ analytics: true });
  assert.equal(notified, 2, "unsubscribed listener must not fire");
});

test("store.resetKey: restores a single field only", () => {
  const store = createSettingsStore(memStore());
  store.set({ analytics: true, weeklyDigest: true });
  store.resetKey("analytics");
  assert.equal(store.get().analytics, SETTINGS_DEFAULTS.analytics);
  assert.equal(store.get().weeklyDigest, true);
});

test("store.getSnapshot is referentially stable until a change", () => {
  const store = createSettingsStore(memStore());
  const a = store.get();
  assert.equal(store.get(), a, "no-op reads return the same reference (useSyncExternalStore-safe)");
  store.set({ analytics: !a.analytics });
  assert.notEqual(store.get(), a);
});
