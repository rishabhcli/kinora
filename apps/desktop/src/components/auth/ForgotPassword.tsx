// ForgotPassword — the "reset your password" panel. Validates the email and
// calls the (graceful) reset endpoint, then shows a neutral confirmation that
// never reveals whether the address has an account (no enumeration).
import { useState, type FormEvent } from "react";
import Field from "./Field";
import AuthIcon from "./AuthIcon";
import { validateEmail } from "./validation";
import { requestPasswordReset } from "../../lib/api/account";

interface Props {
  /** Prefill from the sign-in form. */
  initialEmail?: string;
  onBack: () => void;
}

export default function ForgotPassword({ initialEmail = "", onBack }: Props) {
  const [email, setEmail] = useState(initialEmail);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  const error = touched ? validateEmail(email) : null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (validateEmail(email)) return;
    setBusy(true);
    await requestPasswordReset(email.trim());
    setBusy(false);
    setSent(true);
  }

  if (sent) {
    return (
      <div className="auth-form">
        <div className="auth-formmsg auth-formmsg--info" role="status">
          <AuthIcon name="check" size={16} brand={false} />
          <span>
            If an account exists for <strong>{email.trim()}</strong>, a reset link is on its way.
          </span>
        </div>
        <div className="auth-switch" style={{ marginTop: 18 }}>
          <button type="button" className="auth-link auth-link--strong" onClick={onBack}>
            Back to sign in
          </button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="auth-form" noValidate>
      <p className="auth-card-sub" style={{ marginBottom: 16 }}>
        Enter your email and we'll send a link to reset your password.
      </p>
      <Field
        id="reset-email"
        label="Email address"
        icon="mail"
        type="email"
        autoComplete="email"
        autoFocus
        placeholder="you@example.com"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        onBlur={() => setTouched(true)}
        error={error}
        showError={Boolean(error)}
      />
      <button type="submit" className="auth-submit" disabled={busy}>
        <span className="auth-submit-label">
          {busy ? <AuthIcon name="loader" size={17} className="auth-spin" /> : "Send reset link"}
        </span>
      </button>
      <div className="auth-switch" style={{ marginTop: 18 }}>
        <button type="button" className="auth-link" onClick={onBack}>
          Back to sign in
        </button>
      </div>
    </form>
  );
}
