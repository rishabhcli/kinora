import { describe, expect, it } from "vitest";

import { LadderTelemetry } from "./ladderTelemetry";

/** A controllable clock so the time-based metrics are deterministic. */
function clock(): { now: () => number; advance: (ms: number) => void } {
  let t = 0;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

describe("LadderTelemetry", () => {
  it("accumulates time-in-rung and counts transitions (open rung counted live)", () => {
    const c = clock();
    const tel = new LadderTelemetry({ now: c.now });
    tel.observe({ currentStage: "audio_text_only", playheadSeekSeq: 0 }); // seed @0
    c.advance(1000);
    tel.observe({ currentStage: "keyframe_ken_burns", playheadSeekSeq: 0 }); // @1000
    c.advance(2000);
    tel.observe({ currentStage: "full_video", playheadSeekSeq: 0 }); // @3000
    c.advance(4000);

    const m = tel.getMetrics();
    expect(m.transitions).toBe(2);
    expect(m.msInRung.audio_text_only).toBe(1000);
    expect(m.msInRung.keyframe_ken_burns).toBe(2000);
    expect(m.msInRung.full_video).toBe(4000); // still-open rung billed to now
    expect(m.fullVideoFraction).toBeCloseTo(4000 / 7000, 5);
  });

  it("counts a stall only when transitioning INTO the bare audio-text floor", () => {
    const c = clock();
    const tel = new LadderTelemetry({ now: c.now });
    tel.observe({ currentStage: "full_video", playheadSeekSeq: 0 });
    c.advance(500);
    tel.observe({ currentStage: "audio_text_only", playheadSeekSeq: 0 });
    expect(tel.getMetrics().stalls).toBe(1);
  });

  it("measures a seek that lands straight on a visual rung as ~instant", () => {
    const c = clock();
    const tel = new LadderTelemetry({ now: c.now });
    tel.observe({ currentStage: "full_video", playheadSeekSeq: 0 });
    c.advance(100);
    tel.observe({ currentStage: "keyframe_ken_burns", playheadSeekSeq: 1 }); // seek + cache hit
    expect(tel.getMetrics().lastSeekToFirstFrameMs).toBe(0);
  });

  it("times a seek that lands on the floor until the bridge arrives", () => {
    const c = clock();
    const tel = new LadderTelemetry({ now: c.now });
    tel.observe({ currentStage: "full_video", playheadSeekSeq: 0 });
    c.advance(50);
    tel.observe({ currentStage: "audio_text_only", playheadSeekSeq: 1 }); // seek → floor
    c.advance(120);
    tel.observe({ currentStage: "keyframe_ken_burns", playheadSeekSeq: 1 }); // still arrives
    const m = tel.getMetrics();
    expect(m.lastSeekToFirstFrameMs).toBe(120);
    expect(m.maxSeekToFirstFrameMs).toBe(120);
  });
});
