import { describe, expect, it } from "vitest";

import { parseSessionEvent } from "./events";
import {
  activityFromEvent,
  formatActivityLog,
  groupActivity,
  latestAgent,
  type SessionActivity,
  summarizeFeed,
} from "./feed";

/** Project raw §5.6 payloads into the feed (newest first), mirroring the hook. */
function feed(...raw: unknown[]): SessionActivity[] {
  const items: SessionActivity[] = [];
  raw.forEach((r, i) => {
    const event = parseSessionEvent(r);
    if (!event) return;
    const item = activityFromEvent(event, { id: i, at: 1000 + i * 1000 });
    if (item) items.unshift(item);
  });
  return items;
}

const compose = { event: "agent_activity", agent: "cinematographer", message: "Composing shot shot_1", shot_id: "shot_1" };
const render = { event: "agent_activity", agent: "generator", message: "Rendered shot shot_1", shot_id: "shot_1" };
const qa = {
  event: "agent_activity",
  agent: "critic",
  aspect: "qa",
  message: "Shot shot_1 passed QA — CCS 0.92",
  shot_id: "shot_1",
  qa: { ccs: 0.92, verdict: "pass" },
};

describe("groupActivity", () => {
  it("collapses a shot's compose→render→QA run into one group", () => {
    const items = groupActivity(feed(compose, render, qa));
    expect(items).toHaveLength(1);
    const g = items[0];
    if (g?.type !== "shot") throw new Error("expected a shot group");
    expect(g.shotId).toBe("shot_1");
    expect(g.activities).toHaveLength(3);
  });

  it("keeps a lone lifecycle entry as a single", () => {
    const items = groupActivity(feed(render));
    expect(items[0]?.type).toBe("single");
  });

  it("never groups across a different shot or a conflict, and leaves budget/scene single", () => {
    const items = groupActivity(
      feed(
        compose,
        render, // shot_1 run (2)
        { event: "conflict_choice", conflict_id: "cf1", shot_id: "shot_1", options: [] },
        { event: "agent_activity", agent: "generator", message: "Rendered shot shot_2", shot_id: "shot_2" },
        { event: "budget_low", remaining_s: 30 },
      ),
    );
    // newest-first: [budget, shot_2(single), conflict(single), shot_1 group(2)]
    expect(items.map((i) => i.type)).toEqual(["single", "single", "single", "shot"]);
    const group = items[3];
    if (group?.type !== "shot") throw new Error("expected trailing shot group");
    expect(group.activities).toHaveLength(2);
  });
});

describe("summarizeFeed", () => {
  it("rolls up renders, QA, CCS, conflicts, budget and scenes", () => {
    const s = summarizeFeed(
      feed(
        compose,
        render,
        qa, // 1 render (generator) + 1 qaCheck pass, ccs 0.92
        { event: "regen_done", shot_id: "shot_2", oss_url: "u", qa: { ccs: 0.8, verdict: "fail" } },
        { event: "conflict_choice", conflict_id: "cf1", shot_id: "shot_9", options: [] },
        { event: "regen_done", shot_id: "shot_9" }, // resolves cf1 (same shot, later)
        { event: "budget_low", remaining_s: 12 },
        { event: "scene_stitched", scene_id: "scene_1" },
      ),
    );
    expect(s.renders).toBe(3); // generator(1) + regen(2)
    expect(s.qaChecks).toBe(1);
    expect(s.qaPass).toBe(1);
    expect(s.qaFail).toBe(1); // the failing regen qa
    expect(s.avgCcs).toBeCloseTo((0.92 + 0.8) / 2, 5);
    expect(s.conflictsRaised).toBe(1);
    expect(s.conflictsResolved).toBe(1);
    expect(s.budgetWarnings).toBe(1);
    expect(s.scenesStitched).toBe(1);
  });

  it("returns null avgCcs when no structured QA is present", () => {
    expect(summarizeFeed(feed(compose)).avgCcs).toBeNull();
  });
});

describe("latestAgent", () => {
  it("returns the newest agent entry", () => {
    expect(latestAgent(feed(compose, render))?.role).toBe("generator");
    expect(latestAgent(feed({ event: "budget_low", remaining_s: 5 }))).toBeNull();
  });
});

describe("formatActivityLog", () => {
  it("renders an oldest→newest Markdown transcript", () => {
    const log = formatActivityLog(feed(compose, render));
    expect(log).toContain("# Kinora — crew activity");
    const composeIdx = log.indexOf("Cinematographer");
    const renderIdx = log.indexOf("Generator");
    expect(composeIdx).toBeGreaterThan(-1);
    expect(renderIdx).toBeGreaterThan(composeIdx); // oldest first
  });
});
