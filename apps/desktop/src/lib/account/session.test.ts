import { describe, it, expect } from "vitest";
import { memoryStore } from "./store";
import {
  parseSession,
  parseSessions,
  sessionLabel,
  deviceKindFromUA,
  sortSessions,
  currentSession,
  revocableSessions,
  otherDeviceCount,
  isStale,
  relativeTime,
  removeSession,
  keepOnlyCurrent,
  createSessionCache,
  type DeviceSession,
} from "./session";

function sess(over: Partial<DeviceSession> = {}): DeviceSession {
  return {
    id: over.id ?? "s1",
    kind: over.kind ?? "web",
    createdAt: over.createdAt ?? 1_000,
    lastSeenAt: over.lastSeenAt ?? 2_000,
    ...over,
  };
}

describe("parseSession", () => {
  it("requires an id", () => {
    expect(parseSession({ kind: "web" })).toBeNull();
    expect(parseSession(null)).toBeNull();
    expect(parseSession("x")).toBeNull();
  });
  it("maps snake_case and ISO timestamps", () => {
    const s = parseSession({
      id: "abc",
      device_kind: "mobile",
      created_at: "2023-01-01T00:00:00Z",
      last_seen_at: "2023-01-02T00:00:00Z",
      is_current: true,
    });
    expect(s).toMatchObject({ id: "abc", kind: "mobile", current: true });
    expect(s!.createdAt).toBe(Date.parse("2023-01-01T00:00:00Z"));
    expect(s!.lastSeenAt).toBe(Date.parse("2023-01-02T00:00:00Z"));
  });
  it("defaults an unknown kind and lastSeen to createdAt", () => {
    const s = parseSession({ id: "x", kind: "weird", createdAt: 50 })!;
    expect(s.kind).toBe("unknown");
    expect(s.lastSeenAt).toBe(50);
  });
});

describe("parseSessions", () => {
  it("drops malformed rows", () => {
    expect(parseSessions([{ id: "a" }, { nope: 1 }, "x"]).map((s) => s.id)).toEqual(["a"]);
    expect(parseSessions("nope")).toEqual([]);
  });
});

describe("sessionLabel", () => {
  it("prefers explicit label, then platform+client, then kind noun", () => {
    expect(sessionLabel(sess({ label: "My Mac" }))).toBe("My Mac");
    expect(sessionLabel(sess({ platform: "macOS", client: "Chrome" }))).toBe("macOS · Chrome");
    expect(sessionLabel(sess({ kind: "mobile" }))).toBe("Phone");
  });
});

describe("deviceKindFromUA", () => {
  it("classifies common UAs", () => {
    expect(deviceKindFromUA("Mozilla/5.0 (iPhone; CPU iPhone OS) Mobile")).toBe("mobile");
    expect(deviceKindFromUA("Mozilla/5.0 (iPad)")).toBe("tablet");
    expect(deviceKindFromUA("Kinora/0.0.1 Electron")).toBe("desktop");
    expect(deviceKindFromUA("Mozilla/5.0 Chrome Safari")).toBe("web");
    expect(deviceKindFromUA("curl/8")).toBe("unknown");
  });
});

describe("ordering & queries", () => {
  const list = [
    sess({ id: "old", lastSeenAt: 100 }),
    sess({ id: "cur", current: true, lastSeenAt: 50 }),
    sess({ id: "new", lastSeenAt: 300 }),
  ];
  it("pins current first, then most recent", () => {
    expect(sortSessions(list).map((s) => s.id)).toEqual(["cur", "new", "old"]);
  });
  it("finds the current session", () => {
    expect(currentSession(list)?.id).toBe("cur");
  });
  it("revocable excludes the current device", () => {
    expect(revocableSessions(list).map((s) => s.id).sort()).toEqual(["new", "old"]);
    expect(otherDeviceCount(list)).toBe(2);
  });
});

describe("isStale", () => {
  it("flags sessions unseen past the window", () => {
    const now = 100 * 86_400_000;
    expect(isStale(sess({ lastSeenAt: now - 40 * 86_400_000 }), 30, now)).toBe(true);
    expect(isStale(sess({ lastSeenAt: now - 10 * 86_400_000 }), 30, now)).toBe(false);
  });
});

describe("relativeTime", () => {
  const now = 1_000_000_000_000;
  it("renders coarse buckets", () => {
    expect(relativeTime(now, now)).toBe("just now");
    expect(relativeTime(now - 5 * 60_000, now)).toBe("5 min ago");
    expect(relativeTime(now - 3 * 3_600_000, now)).toBe("3 hr ago");
    expect(relativeTime(now - 86_400_000, now)).toBe("yesterday");
    expect(relativeTime(now - 3 * 86_400_000, now)).toBe("3 days ago");
    expect(relativeTime(now - 14 * 86_400_000, now)).toBe("2 weeks ago");
    expect(relativeTime(now - 60 * 86_400_000, now)).toBe("2 months ago");
    expect(relativeTime(now - 800 * 86_400_000, now)).toBe("2 years ago");
  });
  it("clamps future timestamps to just now", () => {
    expect(relativeTime(now + 10_000, now)).toBe("just now");
  });
});

describe("optimistic revocation", () => {
  const list = [sess({ id: "a", current: true }), sess({ id: "b" }), sess({ id: "c" })];
  it("removes by id", () => {
    expect(removeSession(list, "b").map((s) => s.id)).toEqual(["a", "c"]);
  });
  it("keepOnlyCurrent keeps the current device", () => {
    expect(keepOnlyCurrent(list).map((s) => s.id)).toEqual(["a"]);
  });
});

describe("createSessionCache", () => {
  it("persists, sorts, revokes, and notifies", () => {
    const backing = memoryStore();
    const cache = createSessionCache(backing);
    let hits = 0;
    const off = cache.subscribe(() => hits++);

    cache.set([sess({ id: "b", lastSeenAt: 1 }), sess({ id: "a", current: true, lastSeenAt: 0 })]);
    expect(cache.list().map((s) => s.id)).toEqual(["a", "b"]); // current pinned
    expect(hits).toBe(1);

    // rehydrates from the same backing
    expect(createSessionCache(backing).list().map((s) => s.id)).toEqual(["a", "b"]);

    cache.revoke("b");
    expect(cache.list().map((s) => s.id)).toEqual(["a"]);
    expect(hits).toBe(2);

    cache.set([sess({ id: "a", current: true }), sess({ id: "x" }), sess({ id: "y" })]);
    cache.revokeOthers();
    expect(cache.list().map((s) => s.id)).toEqual(["a"]);

    off();
    cache.set([]);
    expect(hits).toBe(4); // unsubscribed before this set
  });
});
