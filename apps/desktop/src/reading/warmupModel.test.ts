// Pure warm-up presentation model: the honest step checklist + headline. The
// checklist must read top-to-bottom monotonically (no later step ticked while an
// earlier one is pending) even though the engine paints a bootstrap frame eagerly.
import test from "node:test";
import assert from "node:assert/strict";
import { warmupSteps, warmupHeadline } from "./warmupModel.ts";
import { initialState, type MachineState, type LoadFlags } from "./machine.ts";

function state(over: Omit<Partial<MachineState>, "load"> & { load?: Partial<LoadFlags> }): MachineState {
  const { load, ...rest } = over;
  return { ...initialState, ...rest, load: { ...initialState.load, ...(load || {}) } };
}

test("fallback mode shows two steps", () => {
  const steps = warmupSteps(state({ mode: "fallback", phase: "warming", load: { firstFrame: true } }), false);
  assert.deepEqual(steps.map((s) => s.label), ["Opening the book", "Cueing the film"]);
  assert.equal(steps[1].done, true);
});

test("non-live load shows the four-step path", () => {
  const steps = warmupSteps(state({ phase: "loading", load: { pages: true } }), false);
  assert.deepEqual(steps.map((s) => s.label), ["Opening the book", "Reading the text", "Composing the shots", "Cueing the film"]);
});

test("a live session shows the five-step path", () => {
  const steps = warmupSteps(state({ mode: "live", phase: "warming", load: { pages: true, shots: true, session: true } }), true);
  assert.deepEqual(steps.map((s) => s.label), [
    "Opening the book",
    "Reading the text",
    "Composing the shots",
    "Connecting the film",
    "Generating ahead",
  ]);
});

test("the checklist is monotonic — no later step ticks while an earlier one is pending", () => {
  const steps = warmupSteps(state({ phase: "loading", load: { pages: true, shots: false, firstFrame: true } }), false);
  const composing = steps.find((s) => s.label === "Composing the shots");
  const cueing = steps.find((s) => s.label === "Cueing the film");
  assert.equal(composing?.done, false);
  assert.equal(cueing?.done, false); // forced false despite firstFrame, to stay monotonic
});

test("headline is phase- and mode-aware", () => {
  assert.equal(warmupHeadline(state({ mode: "fallback", phase: "warming" })), "Preparing your film");
  assert.equal(warmupHeadline(state({ phase: "opening" })), "Opening the book");
  assert.equal(warmupHeadline(state({ phase: "loading" })), "Reading the story");
  assert.equal(warmupHeadline(state({ mode: "live", phase: "warming" })), "Generating your film");
});
