import { describe, it, expect } from "vitest";
import {
  initialPreviewState,
  reducePreview,
  isPreviewOpen,
  type PreviewState,
  type PreviewConfig,
} from "./preview";

const cfg: PreviewConfig = { openDelayMs: 100, closeDelayMs: 50 };

/** Drive the machine through a sequence of (event, now) steps. */
function drive(
  steps: { ev: Parameters<typeof reducePreview>[1]; now: number }[],
): PreviewState {
  let state = initialPreviewState();
  for (const { ev, now } of steps) {
    state = reducePreview(state, ev, now, cfg).state;
  }
  return state;
}

describe("reducePreview", () => {
  it("opens only after the hover-intent delay", () => {
    let state = initialPreviewState();
    const enter = reducePreview(state, { type: "enter", id: "a" }, 0, cfg);
    state = enter.state;
    expect(enter.armInMs).toBe(100);
    expect(isPreviewOpen(state, "a")).toBe(false);

    // tick too early — still pending, re-arm for the remainder
    const early = reducePreview(state, { type: "tick" }, 60, cfg);
    expect(isPreviewOpen(early.state, "a")).toBe(false);
    expect(early.armInMs).toBe(40);

    // tick after the delay — opens
    const open = reducePreview(early.state, { type: "tick" }, 100, cfg);
    expect(isPreviewOpen(open.state, "a")).toBe(true);
  });

  it("cancels a pending open if the pointer leaves before the delay", () => {
    const state = drive([
      { ev: { type: "enter", id: "a" }, now: 0 },
      { ev: { type: "leave", id: "a" }, now: 40 },
      { ev: { type: "tick" }, now: 200 },
    ]);
    expect(state.openId).toBeNull();
    expect(state.pendingId).toBeNull();
  });

  it("a fast sweep across cards never opens any", () => {
    const state = drive([
      { ev: { type: "enter", id: "a" }, now: 0 },
      { ev: { type: "enter", id: "b" }, now: 20 },
      { ev: { type: "enter", id: "c" }, now: 40 },
      { ev: { type: "leave", id: "c" }, now: 60 },
      { ev: { type: "tick" }, now: 300 },
    ]);
    expect(state.openId).toBeNull();
  });

  it("keeps the preview open while the pointer is inside it", () => {
    // open a, then leave the card but enter the preview panel within the grace
    let r = reducePreview(initialPreviewState(), { type: "enter", id: "a" }, 0, cfg);
    r = reducePreview(r.state, { type: "tick" }, 100, cfg); // open
    r = reducePreview(r.state, { type: "leave", id: "a" }, 110, cfg); // start closing
    expect(r.state.closingSince).toBe(110);
    r = reducePreview(r.state, { type: "enterPreview" }, 120, cfg); // cancel close
    expect(r.state.closingSince).toBeNull();
    r = reducePreview(r.state, { type: "tick" }, 500, cfg);
    expect(isPreviewOpen(r.state, "a")).toBe(true);
  });

  it("closes after the grace once pointer leaves both card and preview", () => {
    let r = reducePreview(initialPreviewState(), { type: "enter", id: "a" }, 0, cfg);
    r = reducePreview(r.state, { type: "tick" }, 100, cfg); // open
    r = reducePreview(r.state, { type: "leavePreview" }, 110, cfg); // start closing
    expect(r.armInMs).toBe(50);
    r = reducePreview(r.state, { type: "tick" }, 160, cfg); // grace elapsed
    expect(r.state.openId).toBeNull();
  });

  it("re-entering the open card cancels its close", () => {
    let r = reducePreview(initialPreviewState(), { type: "enter", id: "a" }, 0, cfg);
    r = reducePreview(r.state, { type: "tick" }, 100, cfg);
    r = reducePreview(r.state, { type: "leave", id: "a" }, 110, cfg);
    expect(r.state.closingSince).toBe(110);
    r = reducePreview(r.state, { type: "enter", id: "a" }, 120, cfg);
    expect(r.state.closingSince).toBeNull();
    expect(isPreviewOpen(r.state, "a")).toBe(true);
  });

  it("dismiss resets everything", () => {
    let r = reducePreview(initialPreviewState(), { type: "enter", id: "a" }, 0, cfg);
    r = reducePreview(r.state, { type: "tick" }, 100, cfg);
    r = reducePreview(r.state, { type: "dismiss" }, 110, cfg);
    expect(r.state).toEqual(initialPreviewState());
  });

  it("switching cards moves the preview to the new card after its delay", () => {
    let r = reducePreview(initialPreviewState(), { type: "enter", id: "a" }, 0, cfg);
    r = reducePreview(r.state, { type: "tick" }, 100, cfg); // a open
    r = reducePreview(r.state, { type: "enter", id: "b" }, 110, cfg); // pending b
    r = reducePreview(r.state, { type: "tick" }, 210, cfg); // b open delay elapsed
    expect(isPreviewOpen(r.state, "b")).toBe(true);
  });
});
