import { describe, it, expect } from "vitest";
import { memoryStore } from "./store";
import {
  DEFAULT_PREFERENCES,
  mergePreferences,
  createPreferencesStore,
  PREFERENCES_STORAGE_KEY,
} from "./preferences";

describe("mergePreferences", () => {
  it("returns defaults for non-objects", () => {
    expect(mergePreferences(null)).toEqual(DEFAULT_PREFERENCES);
    expect(mergePreferences("x")).toEqual(DEFAULT_PREFERENCES);
  });
  it("coerces each field and ignores unknown keys", () => {
    const p = mergePreferences({
      email: { product: false, digest: "monthly", junk: 1 },
      privacy: { visibility: "public", analytics: "nope" },
      extra: true,
    });
    expect(p.email.product).toBe(false);
    expect(p.email.digest).toBe("monthly");
    expect(p.email.security).toBe(true); // default kept
    expect(p.privacy.visibility).toBe("public");
    expect(p.privacy.analytics).toBe(true); // bad type → default
  });
  it("rejects invalid enum values", () => {
    expect(mergePreferences({ email: { digest: "daily" } }).email.digest).toBe("weekly");
    expect(mergePreferences({ privacy: { visibility: "secret" } }).privacy.visibility).toBe("private");
  });
});

describe("createPreferencesStore", () => {
  it("patches a single field without disturbing others", () => {
    const store = createPreferencesStore(memoryStore());
    const next = store.patch({ email: { product: false } });
    expect(next.email.product).toBe(false);
    expect(next.email.render).toBe(true);
    expect(store.get().email.product).toBe(false);
  });

  it("persists, rehydrates, notifies, and resets", () => {
    const backing = memoryStore();
    const store = createPreferencesStore(backing);
    let hits = 0;
    const off = store.subscribe(() => hits++);

    store.patch({ push: { weeklyStreak: true } });
    expect(hits).toBe(1);
    expect(backing.getItem(PREFERENCES_STORAGE_KEY)).toContain("weeklyStreak");

    // rehydrate
    expect(createPreferencesStore(backing).get().push.weeklyStreak).toBe(true);

    store.reset();
    expect(store.get()).toEqual(DEFAULT_PREFERENCES);
    expect(hits).toBe(2);

    off();
    store.patch({ email: { product: false } });
    expect(hits).toBe(2);
  });

  it("set() runs through merge (sanitizes)", () => {
    const store = createPreferencesStore(memoryStore());
    store.set({ ...DEFAULT_PREFERENCES, email: { ...DEFAULT_PREFERENCES.email, digest: "bogus" as never } });
    expect(store.get().email.digest).toBe("weekly");
  });
});
