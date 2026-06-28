// OnboardingFlow — the guided first-run experience, shown once after a reader's
// first sign-in (§5.1). A modal-overlay stepper driven by the pure onboarding
// step machine (lib/account/onboarding): welcome → profile → taste →
// library → notifications → done. Resumable (the store persists progress) and
// fully skippable. Each step is a thin renderer over the shared state; the
// component reports completion via onFinish so the host can route into the app.
import { useEffect, useState } from "react";
import { ArrowRight, ArrowLeft, X } from "lucide-react";
import "../account/account.css";
import {
  type OnboardingState,
  type OnboardingStepId,
  createOnboardingStore,
  currentStep,
  onboardingProgress,
  isFirstStep,
  canSkipCurrent,
} from "../../lib/account";
import { WelcomeStep } from "./steps/WelcomeStep";
import { ProfileStep } from "./steps/ProfileStep";
import { TasteStep } from "./steps/TasteStep";
import { LibraryStep } from "./steps/LibraryStep";
import { NotificationsStep } from "./steps/NotificationsStep";
import { DoneStep } from "./steps/DoneStep";

interface Props {
  /** Called once the reader finishes (or skips to the end). */
  onFinish: () => void;
  /** Optional seed email for the profile step. */
  email?: string;
}

export default function OnboardingFlow({ onFinish, email }: Props) {
  const [store] = useState(() => createOnboardingStore());
  const [state, setState] = useState<OnboardingState>(() => store.get());

  useEffect(() => store.subscribe(() => setState(store.get())), [store]);

  const step = currentStep(state);
  const progress = onboardingProgress(state);

  function next() {
    if (step.id === "done") {
      store.finish();
      onFinish();
      return;
    }
    store.advance();
  }

  function finishNow() {
    store.finish();
    onFinish();
  }

  const stepNumber = Math.round(progress * 5) + 1;

  return (
    <div className="onb-overlay" role="dialog" aria-modal="true" aria-label="Welcome to Kinora">
      <div className="onb-card">
        <div className="onb-progress" aria-hidden="true">
          <div className="onb-progress-fill" style={{ width: `${progress * 100}%` }} />
        </div>

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span className="onb-step-kicker">
            Step {Math.min(6, stepNumber)} of 6
          </span>
          {step.id !== "done" && (
            <button type="button" className="acct-btn acct-btn--ghost" aria-label="Skip setup" onClick={finishNow}>
              <X size={15} /> Skip
            </button>
          )}
        </div>

        <h2 className="onb-title">{step.title}</h2>
        {step.blurb && <p className="onb-blurb">{step.blurb}</p>}

        <div className="onb-body">
          <StepBody id={step.id} email={email} />
        </div>

        <div className="onb-actions">
          <div>
            {!isFirstStep(state) && step.id !== "done" && (
              <button type="button" className="acct-btn acct-btn--ghost" onClick={() => store.back()}>
                <ArrowLeft size={15} /> Back
              </button>
            )}
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            {canSkipCurrent(state) && step.id !== "done" && (
              <button type="button" className="acct-btn acct-btn--ghost" onClick={() => store.skip()}>
                Skip this
              </button>
            )}
            <button type="button" className="acct-btn acct-btn--primary" onClick={next}>
              {step.id === "done" ? "Enter Kinora" : "Continue"}
              {step.id !== "done" && <ArrowRight size={15} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function StepBody({ id, email }: { id: OnboardingStepId; email?: string }) {
  switch (id) {
    case "welcome":
      return <WelcomeStep />;
    case "profile":
      return <ProfileStep email={email} />;
    case "taste":
      return <TasteStep />;
    case "library":
      return <LibraryStep />;
    case "notifications":
      return <NotificationsStep />;
    case "done":
      return <DoneStep />;
    default:
      return null;
  }
}
