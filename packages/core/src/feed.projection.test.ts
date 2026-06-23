import { describe, expect, it } from "vitest";

import { parseSessionEvent } from "./events";
import {
  activityFromEvent,
  agentRoleLabel,
  normalizeAgentRole,
  shortShotId,
  summarizeQa,
  type SessionActivity,
} from "./feed";

/** Project one raw §5.6 payload into a feed entry (or null if it doesn't surface). */
function project(raw: unknown, previousClipUrl?: string | null): SessionActivity | null {
  const event = parseSessionEvent(raw);
  if (!event) return null;
  return activityFromEvent(event, { id: 1, at: 1000, previousClipUrl });
}

describe("normalizeAgentRole", () => {
  it("maps the six crew names (incl. snake_case + display forms)", () => {
    expect(normalizeAgentRole("showrunner")).toBe("showrunner");
    expect(normalizeAgentRole("adapter")).toBe("adapter");
    expect(normalizeAgentRole("continuity_supervisor")).toBe("continuity");
    expect(normalizeAgentRole("Continuity")).toBe("continuity");
    expect(normalizeAgentRole("cinematographer")).toBe("cinematographer");
    expect(normalizeAgentRole("generator")).toBe("generator");
    expect(normalizeAgentRole("critic")).toBe("critic");
  });

  it("degrades unknown/empty to a labelled fallback rather than throwing", () => {
    expect(normalizeAgentRole("mystery_bot")).toBe("unknown");
    expect(normalizeAgentRole(null)).toBe("unknown");
    expect(agentRoleLabel(normalizeAgentRole(undefined))).toBe("Crew");
  });
});

describe("activityFromEvent — non-conflict projections", () => {
  it("tags an agent_activity with its role + aspect", () => {
    const item = project({
      event: "agent_activity",
      agent: "cinematographer",
      aspect: "pacing",
      message: "holding the wide a beat longer",
      shot_id: "shot_12",
    });
    if (item?.kind !== "agent") throw new Error("wrong kind");
    expect(item.role).toBe("cinematographer");
    expect(item.aspect).toBe("pacing");
    expect(item.shotId).toBe("shot_12");
  });

  it("captures before/after clips on regen_done", () => {
    const item = project(
      { event: "regen_done", shot_id: "shot_7", oss_url: "https://oss/after.mp4", qa: { ccs: 0.93, verdict: "pass" } },
      "https://oss/before.mp4",
    );
    if (item?.kind !== "regen") throw new Error("wrong kind");
    expect(item.beforeUrl).toBe("https://oss/before.mp4");
    expect(item.afterUrl).toBe("https://oss/after.mp4");
  });

  it("projects budget_low and scene_stitched", () => {
    const budget = project({ event: "budget_low", remaining_s: 42 });
    expect(budget?.kind === "budget" && budget.remainingS).toBe(42);
    const scene = project({ event: "scene_stitched", scene_id: "scene_3" });
    expect(scene?.kind === "scene" && scene.sceneId).toBe("scene_3");
  });

  it("does not surface clip/keyframe hot-swaps in the feed", () => {
    expect(project({ event: "clip_ready", shot_id: "shot_1" })).toBeNull();
    expect(project({ event: "keyframe_ready", beat_id: "beat_1" })).toBeNull();
  });
});

describe("summarizeQa", () => {
  it("reads ccs + verdict (string enum) into a badge summary", () => {
    expect(summarizeQa({ ccs: 0.91, score: 0.8, verdict: "pass" })).toEqual({
      ccs: 0.91,
      score: 0.8,
      passed: true,
    });
    expect(summarizeQa({ ccs: 0.4, verdict: "fail" })?.passed).toBe(false);
  });

  it("tolerates a boolean verdict and missing fields", () => {
    expect(summarizeQa({ verdict: true })?.passed).toBe(true);
    expect(summarizeQa({})).toEqual({ ccs: null, score: null, passed: null });
    expect(summarizeQa(null)).toBeNull();
  });
});

describe("shortShotId", () => {
  it("strips the shot prefix and truncates", () => {
    expect(shortShotId("shot_ab12cd34ef")).toBe("ab12cd34");
    expect(shortShotId("plain")).toBe("plain");
  });
});
