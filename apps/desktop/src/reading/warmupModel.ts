// Pure presentation model for the WarmUp affordance — the honest step checklist
// + the phase/mode-aware headline. Kept framework-free + unit-tested so the
// checklist always reads monotonically (an eager bootstrap frame can set
// firstFrame before the shots exist; the display must not tick out of order).
// (Named *Model to avoid a case-only filename clash with WarmUp.tsx on
// case-insensitive filesystems.)
import type { MachineState } from "./machine";

export interface WarmStep {
  label: string;
  done: boolean;
}

/** Once a step is pending, every later step shows pending too. */
function monotonic(steps: WarmStep[]): WarmStep[] {
  let pendingSeen = false;
  return steps.map((s) => {
    if (pendingSeen) return { ...s, done: false };
    if (!s.done) pendingSeen = true;
    return s;
  });
}

export function warmupSteps(state: MachineState, live: boolean): WarmStep[] {
  const { load, mode } = state;
  if (mode === "fallback") {
    return monotonic([
      { label: "Opening the book", done: true },
      { label: "Cueing the film", done: load.firstFrame },
    ]);
  }
  const steps: WarmStep[] = [
    { label: "Opening the book", done: true },
    { label: "Reading the text", done: load.pages },
    { label: "Composing the shots", done: load.shots },
  ];
  if (live || load.session) {
    steps.push({ label: "Connecting the film", done: load.session });
    steps.push({ label: "Generating ahead", done: load.firstFrame });
  } else {
    steps.push({ label: "Cueing the film", done: load.firstFrame });
  }
  return monotonic(steps);
}

export function warmupHeadline(state: MachineState): string {
  if (state.mode === "fallback") return "Preparing your film";
  switch (state.phase) {
    case "opening":
      return "Opening the book";
    case "loading":
      return "Reading the story";
    case "warming":
      return state.mode === "live" ? "Generating your film" : "Preparing your film";
    default:
      return "Almost ready";
  }
}
