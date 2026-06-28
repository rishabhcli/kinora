// MfaEnrollDialog — the TOTP enrollment flow, driven by the pure enrollReducer
// (lib/account/mfa) and the API adapter (lib/api/account). Steps: choose method
// → scan secret → verify a code → save recovery codes → done. The secret + the
// recovery codes come from the backend when live, else a local demo set so the
// flow is fully exercisable offline.
import { useReducer, useState } from "react";
import AuthIcon from "../auth/AuthIcon";
import Field from "../auth/Field";
import {
  enrollReducer,
  initialEnrollState,
  enrollProgress,
  isValidCodeShape,
  formatSecret,
  recoveryCodesText,
  type RecoveryCodeSet,
} from "../../lib/account";
import {
  beginTotpEnrollment,
  confirmTotpEnrollment,
  type TotpEnrollment,
} from "../../lib/api/account";

interface Props {
  account: string;
  onDone: () => void;
  onCancel: () => void;
}

export default function MfaEnrollDialog({ account, onDone, onCancel }: Props) {
  const [state, dispatch] = useReducer(enrollReducer, initialEnrollState);
  const [enrollment, setEnrollment] = useState<TotpEnrollment | null>(null);
  const [recovery, setRecovery] = useState<RecoveryCodeSet | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [verifyError, setVerifyError] = useState<string | null>(null);

  async function chooseTotp() {
    dispatch({ type: "choose", method: "totp" });
    setBusy(true);
    const e = await beginTotpEnrollment(account);
    setEnrollment(e);
    setBusy(false);
    dispatch({ type: "secretShown" });
  }

  async function verify() {
    if (!isValidCodeShape(code)) {
      setVerifyError("Enter the 6-digit code.");
      return;
    }
    setBusy(true);
    setVerifyError(null);
    const res = await confirmTotpEnrollment(code);
    setBusy(false);
    if (!res.verified) {
      setVerifyError("That code didn't match. Try again.");
      return;
    }
    setRecovery(res.recovery);
    dispatch({ type: "verified" });
  }

  function copyCodes() {
    if (recovery && typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(recoveryCodesText(recovery));
    }
  }

  return (
    <div className="acct-card" role="group" aria-label="Set up two-factor authentication">
      <div className="onb-progress" style={{ marginBottom: 18 }}>
        <div className="onb-progress-fill" style={{ width: `${enrollProgress(state) * 100}%` }} />
      </div>

      {state.step === "method" && (
        <>
          <h3 className="acct-card-title">Add an extra layer of security</h3>
          <p className="acct-card-desc" style={{ marginBottom: 14 }}>
            Use an authenticator app to generate sign-in codes.
          </p>
          <div style={{ display: "flex", gap: 10 }}>
            <button type="button" className="acct-btn acct-btn--primary" disabled={busy} onClick={chooseTotp}>
              Authenticator app
            </button>
            <button type="button" className="acct-btn acct-btn--ghost" onClick={onCancel}>
              Cancel
            </button>
          </div>
        </>
      )}

      {state.step === "scan" && (
        <p className="acct-card-desc">Preparing your secret…</p>
      )}

      {state.step === "verify" && enrollment && (
        <>
          <h3 className="acct-card-title">Scan or enter this secret</h3>
          <p className="acct-card-desc" style={{ marginBottom: 8 }}>
            Add it to your authenticator app, then enter the 6-digit code it shows.
          </p>
          <p className="acct-secret" aria-label="Setup secret">
            {formatSecret(enrollment.secret)}
          </p>
          <Field
            id="mfa-enroll-code"
            label="Verification code"
            icon="lock"
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="123 456"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            error={verifyError}
            showError={Boolean(verifyError)}
          />
          <div style={{ display: "flex", gap: 10 }}>
            <button type="button" className="acct-btn acct-btn--primary" disabled={busy} onClick={verify}>
              {busy ? "Verifying…" : "Verify"}
            </button>
            <button type="button" className="acct-btn acct-btn--ghost" onClick={onCancel}>
              Cancel
            </button>
          </div>
        </>
      )}

      {state.step === "recovery" && recovery && (
        <>
          <h3 className="acct-card-title">Save your recovery codes</h3>
          <p className="acct-card-desc">
            Each code works once if you lose your authenticator. Keep them somewhere safe.
          </p>
          <div className="acct-codes">
            {recovery.codes.map((c) => (
              <span key={c}>{c}</span>
            ))}
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <button type="button" className="acct-btn" onClick={copyCodes}>
              <AuthIcon name="check" size={14} brand={false} /> Copy codes
            </button>
            <button
              type="button"
              className="acct-btn acct-btn--primary"
              onClick={() => {
                dispatch({ type: "recoverySaved" });
                onDone();
              }}
            >
              I've saved them
            </button>
          </div>
        </>
      )}
    </div>
  );
}
