// MfaChallenge — the second-factor step shown when sign-in returns
// `mfa_required`. A 6-digit code field with shape validation (lib/account/mfa)
// and a recovery-code fallback. Pure presentation over the useAuth controller;
// submission is handled by the caller.
import { useState, type FormEvent } from "react";
import Field from "./Field";
import AuthIcon from "./AuthIcon";
import { isValidCodeShape, isValidRecoveryCodeShape, normalizeCode } from "../../lib/account";

interface Props {
  onSubmit: (code: string) => void;
  onCancel: () => void;
  busy?: boolean;
  error?: string | null;
}

export default function MfaChallenge({ onSubmit, onCancel, busy, error }: Props) {
  const [mode, setMode] = useState<"code" | "recovery">("code");
  const [value, setValue] = useState("");
  const [touched, setTouched] = useState(false);

  const valid = mode === "code" ? isValidCodeShape(value) : isValidRecoveryCodeShape(value);
  const localError = !valid && touched
    ? mode === "code"
      ? "Enter the 6-digit code."
      : "Enter a recovery code (XXXX-XXXX)."
    : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (!valid) return;
    onSubmit(mode === "code" ? normalizeCode(value) : value.trim());
  }

  return (
    <form onSubmit={submit} className="auth-form" noValidate>
      <p className="auth-card-sub" style={{ marginBottom: 16 }}>
        {mode === "code"
          ? "Enter the code from your authenticator app."
          : "Enter one of your saved recovery codes."}
      </p>

      <Field
        id="mfa-code"
        label={mode === "code" ? "Authentication code" : "Recovery code"}
        icon="lock"
        inputMode={mode === "code" ? "numeric" : "text"}
        autoComplete="one-time-code"
        autoFocus
        placeholder={mode === "code" ? "123 456" : "XXXX-XXXX"}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={() => setTouched(true)}
        error={localError ?? error}
        showError={Boolean(localError) || Boolean(error)}
      />

      <button type="submit" className="auth-submit" disabled={busy}>
        <span className="auth-submit-label">
          {busy ? (
            <AuthIcon name="loader" size={17} className="auth-spin" />
          ) : (
            <>
              Verify
              <AuthIcon name="arrow-right" size={16} className="auth-submit-arrow" />
            </>
          )}
        </span>
      </button>

      <div className="auth-switch">
        <button
          type="button"
          className="auth-link"
          onClick={() => {
            setMode((m) => (m === "code" ? "recovery" : "code"));
            setValue("");
            setTouched(false);
          }}
        >
          {mode === "code" ? "Use a recovery code instead" : "Use your authenticator app"}
        </button>
      </div>
      <div className="auth-switch" style={{ marginTop: 8 }}>
        <button type="button" className="auth-link" onClick={onCancel}>
          Back to sign in
        </button>
      </div>
    </form>
  );
}
