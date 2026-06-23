import { describe, expect, it } from "vitest";

import {
  activityFromEvent,
  conflictOptionLabel,
  conflictResolution,
  decisionOption,
  isConflictResolved,
  selectActiveConflict,
  type SessionActivity,
} from "./feed";
import { parseSessionEvent } from "./events";

/** Project a sequence of raw §5.6 payloads into the feed (newest first). */
function feed(...raw: unknown[]): SessionActivity[] {
  const items: SessionActivity[] = [];
  raw.forEach((r, i) => {
    const event = parseSessionEvent(r);
    if (!event) return;
    const item = activityFromEvent(event, { id: i, at: i });
    if (item) items.unshift(item);
  });
  return items;
}

const SURFACE = {
  event: "conflict_choice",
  conflict_id: "cf_shot_51",
  shot_id: "shot_51",
  raised_by: "continuity_supervisor",
  claim: "the heroine draws a sword she lost",
  canon_fact: "state_hero_sword_001 retired at beat_0034 (sword lost in the river)",
  current_beat: "beat_0039",
  options: [
    { id: "honor_canon", action: "regenerate empty-handed", cost_video_s: 5 },
    { id: "evolve_canon", action: "assert sword reacquired", requires: "textual support" },
  ],
};

describe("activityFromEvent — conflicts (§7.2)", () => {
  it("projects conflict_choice into a structured ConflictActivity", () => {
    const item = feed(SURFACE)[0];
    if (!item || item.kind !== "conflict") throw new Error("wrong kind");
    expect(item.conflictId).toBe("cf_shot_51");
    expect(item.shotId).toBe("shot_51");
    expect(item.raisedBy).toBe("continuity_supervisor");
    expect(item.currentBeat).toBe("beat_0039");
    expect(item.options).toHaveLength(2);
    expect(item.options[0]?.cost_video_s).toBe(5);
  });

  it("correlates a Showrunner decision agent_activity to its conflict", () => {
    const item = feed({
      event: "agent_activity",
      agent: "showrunner",
      message: "Director chose to honour canon — regenerating without the sword.",
      shot_id: "shot_51",
      conflict: { conflict_id: "cf_shot_51", option: "honor_canon" },
    })[0];
    if (!item || item.kind !== "agent") throw new Error("wrong kind");
    expect(item.conflict).toBe(true);
    expect(item.conflictId).toBe("cf_shot_51");
    expect(decisionOption(item.decision)).toBe("honor_canon");
  });
});

describe("conflict dispute selectors", () => {
  it("surfaces the newest unresolved conflict and hides it once the shot regens", () => {
    const open = feed(SURFACE);
    expect(selectActiveConflict(open)?.conflictId).toBe("cf_shot_51");

    const closed = feed(SURFACE, { event: "regen_done", shot_id: "shot_51" });
    const conflict = closed.find((a) => a.kind === "conflict");
    if (!conflict || conflict.kind !== "conflict") throw new Error("no conflict");
    expect(isConflictResolved(closed, conflict)).toBe(true);
    expect(selectActiveConflict(closed)).toBeNull();
  });

  it("respects the dismissed set", () => {
    const open = feed(SURFACE);
    expect(selectActiveConflict(open, new Set(["cf_shot_51"]))).toBeNull();
  });

  it("streams the arbitration reasoning then marks resolved on regen", () => {
    const items = feed(
      SURFACE,
      {
        event: "agent_activity",
        agent: "showrunner",
        message: "Director chose to honour canon.",
        shot_id: "shot_51",
        conflict: { conflict_id: "cf_shot_51", option: "honor_canon", reasoning: "Honouring canon; regenerating empty-handed." },
      },
      { event: "regen_done", shot_id: "shot_51" },
    );
    const conflict = items.find((a) => a.kind === "conflict");
    if (!conflict || conflict.kind !== "conflict") throw new Error("no conflict");
    const trace = conflictResolution(items, conflict);
    expect(trace.chosen).toBe("honor_canon");
    expect(trace.reasoning).toContain("Honouring canon; regenerating empty-handed.");
    expect(trace.resolved).toBe(true);
  });

  it("labels policy options for the UI", () => {
    expect(conflictOptionLabel("honor_canon")).toBe("honour canon");
    expect(conflictOptionLabel("evolve_canon")).toBe("evolve canon");
    expect(conflictOptionLabel("surface_to_user")).toBe("ask the director");
  });
});
