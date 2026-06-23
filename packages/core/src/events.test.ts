import { describe, expect, it } from "vitest";

import { parseSessionEvent } from "./events";

describe("parseSessionEvent — conflict negotiation (§7.2)", () => {
  it("parses a surfaced conflict_choice with structured options", () => {
    const event = parseSessionEvent({
      event: "conflict_choice",
      conflict_id: "cf_shot_00051",
      shot_id: "shot_00051",
      raised_by: "continuity_supervisor",
      claim: "shot depicts the heroine drawing a sword",
      canon_fact: "state_hero_sword_001 retired at beat_0034 (sword lost in the river)",
      current_beat: "beat_0039",
      options: [
        { id: "honor_canon", action: "regenerate empty-handed", cost_video_s: 5 },
        { id: "surface_to_user", action: "ask the director to choose", cost_video_s: 0 },
        { id: "evolve_canon", action: "assert sword reacquired", requires: "textual support" },
      ],
    });

    expect(event?.event).toBe("conflict_choice");
    if (event?.event !== "conflict_choice") throw new Error("wrong event");
    expect(event.conflict_id).toBe("cf_shot_00051");
    expect(event.claim).toContain("drawing a sword");
    expect(event.canon_fact).toContain("lost in the river");
    expect(event.options).toHaveLength(3);
    expect(event.options[0]).toMatchObject({ id: "honor_canon", cost_video_s: 5 });
    expect(event.options[2]?.requires).toBe("textual support");
  });

  it("tolerates a missing options array and an unknown option id", () => {
    const event = parseSessionEvent({
      event: "conflict_choice",
      conflict_id: "cf_x",
      options: [{ id: "future_policy", action: "do something new" }],
    });
    if (event?.event !== "conflict_choice") throw new Error("wrong event");
    expect(event.options[0]?.id).toBe("future_policy");
    expect(event.claim ?? null).toBeNull();
  });

  it("parses an agent_activity carrying a director-choice decision record", () => {
    const event = parseSessionEvent({
      event: "agent_activity",
      agent: "showrunner",
      message: "conflict cf_shot_00051 resolved: honor_canon",
      shot_id: "shot_00051",
      conflict: { conflict_id: "cf_shot_00051", option: "honor_canon" },
    });
    if (event?.event !== "agent_activity") throw new Error("wrong event");
    expect(event.conflict?.option).toBe("honor_canon");
    expect(event.conflict?.conflict_id).toBe("cf_shot_00051");
  });

  it("parses an agent_activity carrying an auto-arbitrated DecisionRecord", () => {
    const event = parseSessionEvent({
      event: "agent_activity",
      agent: "showrunner",
      message: "Honouring canon (no director present).",
      conflict: {
        conflict_id: "cf_shot_00051",
        chosen_option: "honor_canon",
        reasoning: "No textual support; honouring established canon.",
        evolved_canon: false,
      },
    });
    if (event?.event !== "agent_activity") throw new Error("wrong event");
    expect(event.conflict?.chosen_option).toBe("honor_canon");
    expect(event.conflict?.reasoning).toContain("honouring established canon");
  });
});

describe("parseSessionEvent — buffer_state (§5.3)", () => {
  it("parses a live buffer_state with watermarks, zone, and flags", () => {
    const event = parseSessionEvent({
      event: "buffer_state",
      committed_seconds_ahead: 62.5,
      low: 25,
      high: 75,
      commit_horizon: 45,
      bursting: true,
      idle: false,
      zone: "committed",
      eta_next_s: 2.5,
      velocity_wps: 4,
      inflight_committed: 4,
      inflight_speculative: 2,
      promoted: 3,
      budget_remaining_s: 1200,
    });
    if (event?.event !== "buffer_state") throw new Error("wrong event");
    expect(event.committed_seconds_ahead).toBe(62.5);
    expect(event.high).toBe(75);
    expect(event.commit_horizon).toBe(45);
    expect(event.bursting).toBe(true);
    expect(event.zone).toBe("committed");
    expect(event.eta_next_s).toBe(2.5);
    expect(event.velocity_wps).toBe(4);
    expect(event.inflight_committed).toBe(4);
    expect(event.inflight_speculative).toBe(2);
    expect(event.promoted).toBe(3);
    expect(event.budget_remaining_s).toBe(1200);
  });

  it("defaults flags + zone + promoted and tolerates absent enrichments", () => {
    const event = parseSessionEvent({
      event: "buffer_state",
      committed_seconds_ahead: 0,
      low: 25,
      high: 75,
      commit_horizon: 45,
    });
    if (event?.event !== "buffer_state") throw new Error("wrong event");
    expect(event.bursting).toBe(false);
    expect(event.idle).toBe(false);
    expect(event.zone).toBe("cold");
    expect(event.promoted).toBe(0);
    expect(event.eta_next_s ?? null).toBeNull();
    expect(event.budget_remaining_s ?? null).toBeNull();
  });

  it("rejects an unknown zone", () => {
    expect(
      parseSessionEvent({
        event: "buffer_state",
        committed_seconds_ahead: 10,
        low: 25,
        high: 75,
        commit_horizon: 45,
        zone: "bogus",
      }),
    ).toBeNull();
  });
});
