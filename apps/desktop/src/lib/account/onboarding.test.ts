import { describe, it, expect } from "vitest";
import { memoryStore } from "./store";
import {
  ONBOARDING_STEPS,
  ONBOARDING_STEP_IDS,
  stepDef,
  initialOnboardingState,
  currentStep,
  onboardingProgress,
  isFirstStep,
  isLastStep,
  canSkipCurrent,
  advance,
  skip,
  back,
  goTo,
  finish,
  shouldShowOnboarding,
  createOnboardingStore,
  ONBOARDING_STORAGE_KEY,
  type OnboardingStepId,
} from "./onboarding";

describe("step catalog", () => {
  it("starts at welcome and ends at done", () => {
    expect(ONBOARDING_STEP_IDS[0]).toBe("welcome");
    expect(ONBOARDING_STEP_IDS[ONBOARDING_STEP_IDS.length - 1]).toBe("done");
    expect(stepDef("taste")?.optional).toBe(true);
    expect(stepDef("welcome")?.optional).toBe(false);
  });
});

describe("queries", () => {
  it("reports first/last and progress", () => {
    const s = initialOnboardingState(0);
    expect(isFirstStep(s)).toBe(true);
    expect(onboardingProgress(s)).toBe(0);
    expect(currentStep(s).id).toBe("welcome");
    const last = { ...s, index: ONBOARDING_STEPS.length - 1 };
    expect(isLastStep(last)).toBe(true);
    expect(onboardingProgress(last)).toBe(1);
  });
  it("knows which steps can be skipped", () => {
    expect(canSkipCurrent(initialOnboardingState())).toBe(false); // welcome
    expect(canSkipCurrent({ ...initialOnboardingState(), index: 2 })).toBe(true); // taste
  });
});

describe("advance", () => {
  it("marks the current step complete and moves on", () => {
    let s = initialOnboardingState(0);
    s = advance(s, 1);
    expect(s.index).toBe(1);
    expect(s.completed).toEqual(["welcome"]);
    s = advance(s, 2);
    expect(s.completed).toEqual(["welcome", "profile"]);
  });
  it("does not duplicate completed entries or overrun done", () => {
    let s = { ...initialOnboardingState(), index: ONBOARDING_STEPS.length - 1 };
    s = advance(s);
    expect(s.index).toBe(ONBOARDING_STEPS.length - 1);
  });
});

describe("skip", () => {
  it("records optional skips and advances", () => {
    let s = { ...initialOnboardingState(), index: 2 }; // taste (optional)
    s = skip(s, 5);
    expect(s.skipped).toContain("taste");
    expect(s.index).toBe(3);
  });
  it("treats a required step skip as advance (no skip record)", () => {
    const s = skip(initialOnboardingState()); // welcome (required)
    expect(s.skipped).toEqual([]);
    expect(s.index).toBe(1);
  });
});

describe("back / goTo", () => {
  it("steps back but not past welcome", () => {
    const s = { ...initialOnboardingState(), index: 2 };
    expect(back(s).index).toBe(1);
    expect(back(initialOnboardingState()).index).toBe(0);
  });
  it("goTo allows reached/completed steps only", () => {
    const s = { ...initialOnboardingState(), index: 3, completed: ["welcome", "profile"] as OnboardingStepId[] };
    expect(goTo(s, "profile").index).toBe(1); // completed
    expect(goTo(s, "notifications").index).toBe(3); // ahead, not reached → unchanged
    expect(goTo(s, "bogus" as never).index).toBe(3);
  });
});

describe("finish / shouldShow", () => {
  it("finish jumps to done and sets finished", () => {
    const s = finish(initialOnboardingState());
    expect(s.finished).toBe(true);
    expect(currentStep(s).id).toBe("done");
    expect(shouldShowOnboarding(s)).toBe(false);
  });
  it("an unfinished reader should see onboarding", () => {
    expect(shouldShowOnboarding(initialOnboardingState())).toBe(true);
  });
});

describe("createOnboardingStore", () => {
  it("persists progress, rehydrates, and resumes", () => {
    const backing = memoryStore();
    const store = createOnboardingStore(backing);
    let hits = 0;
    store.subscribe(() => hits++);

    store.advance(); // welcome → profile (completes "welcome")
    store.advance(); // profile → taste (completes "profile")
    expect(store.get().index).toBe(2);
    expect(store.get().completed).toEqual(["welcome", "profile"]);
    expect(hits).toBe(2);
    expect(backing.getItem(ONBOARDING_STORAGE_KEY)).toContain("profile");

    // a fresh store over the same backing resumes at taste
    expect(createOnboardingStore(backing).get().index).toBe(2);

    store.finish();
    expect(store.get().finished).toBe(true);

    store.reset();
    expect(store.get()).toMatchObject({ index: 0, finished: false });
  });

  it("sanitizes a corrupt snapshot", () => {
    const backing = memoryStore({
      [ONBOARDING_STORAGE_KEY]: '{"index":99,"completed":["welcome","bogus"],"finished":1}',
    });
    const st = createOnboardingStore(backing).get();
    expect(st.index).toBe(ONBOARDING_STEPS.length - 1); // clamped
    expect(st.completed).toEqual(["welcome"]); // bogus dropped
    expect(st.finished).toBe(false); // 1 is not true
  });
});
