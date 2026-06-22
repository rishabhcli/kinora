import { beforeEach, describe, expect, it } from "vitest";

import { useEventsStore } from "./eventsStore";

beforeEach(() => {
  useEventsStore.getState().reset();
});

describe("eventsStore", () => {
  it("routes each event type into the right cache + the feed", () => {
    const { push } = useEventsStore.getState();
    push({ type: "keyframe_ready", data: { beat_id: "b1", oss_url: "k1", shot_id: "s1" } });
    push({
      type: "clip_ready",
      data: {
        shot_id: "s1",
        oss_url: "c1",
        sync_segment: {
          shot_id: "s1",
          video_start_s: 0,
          video_end_s: 5,
          page: 1,
          page_turn_at_s: 4.8,
          words: [],
        },
      },
    });
    push({ type: "budget_low", data: { remaining_s: 99 } });
    push({ type: "agent_activity", data: { agent: "Critic", message: "pass" } });
    push({ type: "conflict_choice", data: { conflict_id: "cf1", options: [{ id: "honor", action: "x" }] } });

    const s = useEventsStore.getState();
    expect(s.keyframesByBeat.b1).toBe("k1");
    expect(s.keyframesByShot.s1).toBe("k1");
    expect(s.clips.s1.oss_url).toBe("c1");
    expect(s.budgetRemaining).toBe(99);
    expect(s.agentFeed).toHaveLength(1);
    expect(s.conflicts).toHaveLength(1);
    expect(s.feed).toHaveLength(5);
  });

  it("surfaces an agent_activity-embedded conflict as a conflict card (deduped)", () => {
    const { push } = useEventsStore.getState();
    push({
      type: "agent_activity",
      data: {
        agent: "Continuity",
        message: "Raised a continuity conflict",
        conflict: {
          conflict_id: "cf9",
          claim: "her coat is red",
          canon_fact: "her coat is blue",
          options: [{ id: "honor", action: "Honor canon" }],
        },
      },
    });
    let s = useEventsStore.getState();
    expect(s.agentFeed).toHaveLength(1);
    expect(s.conflicts).toHaveLength(1);
    expect(s.conflicts[0].conflict_id).toBe("cf9");
    expect(s.conflicts[0].options).toHaveLength(1);

    // A later conflict_choice for the same id must not duplicate the card.
    push({ type: "conflict_choice", data: { conflict_id: "cf9", options: [] } });
    s = useEventsStore.getState();
    expect(s.conflicts).toHaveLength(1);
  });

  it("dedupes conflicts by id and resolves them", () => {
    const { push } = useEventsStore.getState();
    push({ type: "conflict_choice", data: { conflict_id: "cf1", options: [] } });
    push({ type: "conflict_choice", data: { conflict_id: "cf1", options: [] } });
    expect(useEventsStore.getState().conflicts).toHaveLength(1);
    useEventsStore.getState().resolveConflict("cf1");
    expect(useEventsStore.getState().conflicts).toHaveLength(0);
  });

  it("tracks connection status", () => {
    useEventsStore.getState().setConnection("open");
    expect(useEventsStore.getState().connection).toBe("open");
  });
});
