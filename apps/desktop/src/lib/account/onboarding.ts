// First-run onboarding (account domain) — an ordered, resumable step machine
// that backs the guided welcome flow shown the first time a reader signs in
// (§5.1: adding a book kicks off ingest; onboarding sets up the reader before
// that). Pure: the ordered step list, completion + skip tracking, navigation,
// progress, and a persisted snapshot so a reload resumes where the reader left
// off. The component is a thin renderer over this state.
import { type KeyValueStore, readJson, resolveStore, writeJson } from "./store";

// ---- Steps ---------------------------------------------------------------- //

export type OnboardingStepId =
  | "welcome" // intro / what Kinora is
  | "profile" // display name + avatar
  | "taste" // pick favourite genres (seeds recommendations)
  | "library" // add a first book / explore demo
  | "notifications" // opt into render + digest emails
  | "done"; // celebratory finish

export interface OnboardingStepDef {
  id: OnboardingStepId;
  title: string;
  /** Optional one-line subtitle. */
  blurb?: string;
  /** Steps the reader may skip (welcome/done are not skippable). */
  optional: boolean;
}

/** The canonical ordered step list. The flow walks these in order; optional
 *  steps can be skipped, required ones must be completed (or are auto on
 *  arrival, like welcome). */
export const ONBOARDING_STEPS: OnboardingStepDef[] = [
  { id: "welcome", title: "Welcome to Kinora", blurb: "Watch the books you read.", optional: false },
  { id: "profile", title: "Make it yours", blurb: "A name and a face for your library.", optional: false },
  { id: "taste", title: "What do you love?", blurb: "Pick a few — we'll tune your shelf.", optional: true },
  { id: "library", title: "Your first film", blurb: "Add a book or explore the demo.", optional: true },
  { id: "notifications", title: "Stay in the loop", blurb: "Only what matters, never noise.", optional: true },
  { id: "done", title: "You're all set", blurb: "Lights down. Enjoy the show.", optional: false },
];

export const ONBOARDING_STEP_IDS: OnboardingStepId[] = ONBOARDING_STEPS.map((s) => s.id);

export function stepDef(id: OnboardingStepId): OnboardingStepDef | undefined {
  return ONBOARDING_STEPS.find((s) => s.id === id);
}

// ---- State ---------------------------------------------------------------- //

export interface OnboardingState {
  /** Index into ONBOARDING_STEPS. */
  index: number;
  /** Steps the reader has completed (advanced past with a real action). */
  completed: OnboardingStepId[];
  /** Optional steps the reader explicitly skipped. */
  skipped: OnboardingStepId[];
  /** Set once the whole flow is finished/dismissed — gates re-showing it. */
  finished: boolean;
  /** When the flow was last touched (epoch ms) — for resume + analytics. */
  updatedAt: number;
}

export function initialOnboardingState(now: number = Date.now()): OnboardingState {
  return { index: 0, completed: [], skipped: [], finished: false, updatedAt: now };
}

export function currentStep(state: OnboardingState): OnboardingStepDef {
  return ONBOARDING_STEPS[clampIndex(state.index)];
}

function clampIndex(i: number): number {
  return Math.max(0, Math.min(ONBOARDING_STEPS.length - 1, i));
}

/** Progress 0..1 (the welcome step is 0, done is 1). */
export function onboardingProgress(state: OnboardingState): number {
  return clampIndex(state.index) / (ONBOARDING_STEPS.length - 1);
}

export function isFirstStep(state: OnboardingState): boolean {
  return clampIndex(state.index) === 0;
}

export function isLastStep(state: OnboardingState): boolean {
  return clampIndex(state.index) === ONBOARDING_STEPS.length - 1;
}

export function canSkipCurrent(state: OnboardingState): boolean {
  return currentStep(state).optional;
}

// ---- Transitions (pure) --------------------------------------------------- //

/** Advance to the next step, marking the current one completed. Stops at done. */
export function advance(state: OnboardingState, now: number = Date.now()): OnboardingState {
  const step = currentStep(state);
  const completed = step.id === "done" || state.completed.includes(step.id)
    ? state.completed
    : [...state.completed, step.id];
  const nextIndex = clampIndex(state.index + 1);
  return {
    ...state,
    index: nextIndex,
    completed,
    finished: ONBOARDING_STEPS[nextIndex].id === "done" ? state.finished : state.finished,
    updatedAt: now,
  };
}

/** Skip the current (optional) step — records it as skipped and advances. A
 *  required step is treated as advance (cannot be skipped). */
export function skip(state: OnboardingState, now: number = Date.now()): OnboardingState {
  const step = currentStep(state);
  if (!step.optional) return advance(state, now);
  const skipped = state.skipped.includes(step.id) ? state.skipped : [...state.skipped, step.id];
  return { ...advance({ ...state, skipped }, now) };
}

/** Step back one (never past welcome). Does not un-complete. */
export function back(state: OnboardingState, now: number = Date.now()): OnboardingState {
  return { ...state, index: clampIndex(state.index - 1), updatedAt: now };
}

/** Jump straight to a step by id (e.g. a stepper click). Only allowed to steps
 *  already reached or the immediate next one. */
export function goTo(state: OnboardingState, id: OnboardingStepId, now: number = Date.now()): OnboardingState {
  const target = ONBOARDING_STEP_IDS.indexOf(id);
  if (target < 0) return state;
  // Allow going to any completed step or anywhere at/behind the current.
  if (target <= state.index || state.completed.includes(id)) {
    return { ...state, index: target, updatedAt: now };
  }
  return state;
}

/** Finish the whole flow (the "done" step's CTA, or a "skip onboarding"). */
export function finish(state: OnboardingState, now: number = Date.now()): OnboardingState {
  return { ...state, index: ONBOARDING_STEPS.length - 1, finished: true, updatedAt: now };
}

/** A reader is "due" for onboarding if they haven't finished it. */
export function shouldShowOnboarding(state: OnboardingState): boolean {
  return !state.finished;
}

// ---- Persistence ---------------------------------------------------------- //

const STORAGE_KEY = "kinora.account.onboarding.v1";

function sanitize(raw: unknown): OnboardingState {
  if (typeof raw !== "object" || raw === null) return initialOnboardingState();
  const r = raw as Record<string, unknown>;
  const ids = new Set<string>(ONBOARDING_STEP_IDS);
  const asIds = (v: unknown): OnboardingStepId[] =>
    Array.isArray(v) ? (v.filter((x) => typeof x === "string" && ids.has(x)) as OnboardingStepId[]) : [];
  return {
    index: typeof r.index === "number" && Number.isFinite(r.index) ? clampIndex(r.index) : 0,
    completed: asIds(r.completed),
    skipped: asIds(r.skipped),
    finished: r.finished === true,
    updatedAt: typeof r.updatedAt === "number" ? r.updatedAt : Date.now(),
  };
}

export interface OnboardingStore {
  get(): OnboardingState;
  set(next: OnboardingState): void;
  /** Convenience mutators returning the new state. */
  advance(): OnboardingState;
  skip(): OnboardingState;
  back(): OnboardingState;
  goTo(id: OnboardingStepId): OnboardingState;
  finish(): OnboardingState;
  reset(): OnboardingState;
  subscribe(fn: () => void): () => void;
}

export function createOnboardingStore(backing?: KeyValueStore | null): OnboardingStore {
  const store = resolveStore(backing);
  let state = sanitize(readJson<unknown>(store, STORAGE_KEY, null));
  const subs = new Set<() => void>();

  const commit = (next: OnboardingState) => {
    state = next;
    writeJson(store, STORAGE_KEY, state);
    subs.forEach((fn) => fn());
    return state;
  };

  return {
    get: () => state,
    set: (next) => void commit(sanitize(next)),
    advance: () => commit(advance(state)),
    skip: () => commit(skip(state)),
    back: () => commit(back(state)),
    goTo: (id) => commit(goTo(state, id)),
    finish: () => commit(finish(state)),
    reset: () => commit(initialOnboardingState()),
    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}

export const ONBOARDING_STORAGE_KEY = STORAGE_KEY;
