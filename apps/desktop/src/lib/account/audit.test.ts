import { describe, it, expect } from "vitest";
import {
  parseSecurityEvent,
  parseSecurityEvents,
  securityEventLabel,
  isAlerting,
  recentAlertCount,
  groupByDay,
  type SecurityEvent,
} from "./audit";

const DAY = 86_400_000;

describe("parseSecurityEvent", () => {
  it("requires an id, maps type/timestamp aliases", () => {
    expect(parseSecurityEvent({ kind: "login" })).toBeNull();
    const e = parseSecurityEvent({ id: "e1", type: "login_failed", timestamp: "2024-01-01T00:00:00Z" })!;
    expect(e.kind).toBe("login_failed");
    expect(e.at).toBe(Date.parse("2024-01-01T00:00:00Z"));
  });
  it("falls back to unknown for an unrecognised kind", () => {
    expect(parseSecurityEvent({ id: "e1", kind: "weird" })!.kind).toBe("unknown");
  });
});

describe("parseSecurityEvents", () => {
  it("drops malformed rows and sorts newest-first", () => {
    const list = parseSecurityEvents([
      { id: "a", kind: "login", at: 100 },
      { bad: 1 },
      { id: "b", kind: "logout", at: 500 },
    ]);
    expect(list.map((e) => e.id)).toEqual(["b", "a"]);
  });
});

describe("labels + alerting", () => {
  it("labels each kind", () => {
    expect(securityEventLabel({ id: "x", kind: "password_changed", at: 0 })).toBe("Password changed");
    expect(securityEventLabel({ id: "x", kind: "unknown", at: 0 })).toBe("Account activity");
  });
  it("flags compromise-signal events", () => {
    expect(isAlerting({ id: "x", kind: "login_failed", at: 0 })).toBe(true);
    expect(isAlerting({ id: "x", kind: "new_device", at: 0 })).toBe(true);
    expect(isAlerting({ id: "x", kind: "login", at: 0 })).toBe(false);
  });
});

describe("recentAlertCount", () => {
  it("counts alerting events within the window", () => {
    const now = 100 * DAY;
    const events: SecurityEvent[] = [
      { id: "a", kind: "login_failed", at: now - 2 * DAY },
      { id: "b", kind: "new_device", at: now - 40 * DAY }, // outside 30d
      { id: "c", kind: "login", at: now - 1 * DAY }, // not alerting
      { id: "d", kind: "recovery_used", at: now - 5 * DAY },
    ];
    expect(recentAlertCount(events, 30, now)).toBe(2);
  });
});

describe("groupByDay", () => {
  it("groups events by UTC day, newest first", () => {
    const events: SecurityEvent[] = [
      { id: "a", kind: "login", at: 0 },
      { id: "b", kind: "logout", at: 1000 }, // same day as a
      { id: "c", kind: "login", at: 2 * DAY + 500 }, // a later day
    ];
    const groups = groupByDay(events);
    expect(groups[0].day).toBe(2);
    expect(groups[1].events.map((e) => e.id)).toEqual(["b", "a"]); // newest-first within day
  });
});
