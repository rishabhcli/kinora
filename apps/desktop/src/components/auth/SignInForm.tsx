// SignInForm — the email+password sign-in form. Validated, a11y-complete (live
// error region, per-field aria-describedby via Field/PasswordField), and wired
// to the useAuth controller. Demo + forgot-password + provider rows are slots
// the parent (LoginPanel) composes around it.
import { useRef, useState, type FormEvent } from "react";
import Field from "./Field";
import PasswordField from "./PasswordField";
import AuthIcon from "./AuthIcon";
import { validateEmail, validatePassword } from "./validation";

export interface SignInFormProps {
  onSubmit: (email: string, password: string) => void;
  busy?: boolean;
  /** A form-level error from the controller (e.g. "Incorrect email or password"). */
  error?: string | null;
  onForgot?: () => void;
  initialEmail?: string;
  initialPassword?: string;
}

export default function SignInForm({
  onSubmit,
  busy,
  error,
  onForgot,
  initialEmail = "",
  initialPassword = "",
}: SignInFormProps) {
  const [email, setEmail] = useState(initialEmail);
  const [password, setPassword] = useState(initialPassword);
  const [touched, setTouched] = useState<{ email: boolean; password: boolean }>({
    email: false,
    password: false,
  });
  const liveRef = useRef<HTMLParagraphElement>(null);

  const emailErr = validateEmail(email);
  const passErr = validatePassword(password, "login");

  function submit(e: FormEvent) {
    e.preventDefault();
    setTouched({ email: true, password: true });
    if (emailErr || passErr) {
      // Nudge the live region so SR users hear the summary.
      if (liveRef.current) liveRef.current.textContent = emailErr ?? passErr ?? "";
      return;
    }
    onSubmit(email.trim(), password);
  }

  return (
    <form onSubmit={submit} className="auth-form" noValidate aria-busy={busy || undefined}>
      <Field
        id="signin-email"
        label="Email address"
        icon="mail"
        type="email"
        autoComplete="email"
        autoFocus
        placeholder="you@example.com"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        onBlur={() => setTouched((t) => ({ ...t, email: true }))}
        error={emailErr}
        showError={touched.email}
      />

      <PasswordField
        id="signin-password"
        label="Password"
        value={password}
        onChange={setPassword}
        onBlur={() => setTouched((t) => ({ ...t, password: true }))}
        error={passErr}
        showError={touched.password}
        autoComplete="current-password"
      />

      {onForgot && (
        <div className="auth-row">
          <span />
          <button type="button" className="auth-link" onClick={onForgot}>
            Forgot password?
          </button>
        </div>
      )}

      {error && (
        <p className="auth-formmsg auth-formmsg--error" role="alert">
          <AuthIcon name="alert" size={15} brand={false} />
          {error}
        </p>
      )}

      <button type="submit" className="auth-submit" disabled={busy}>
        <span className="auth-submit-label">
          {busy ? (
            <AuthIcon name="loader" size={17} className="auth-spin" />
          ) : (
            <>
              Sign in
              <AuthIcon name="arrow-right" size={16} className="auth-submit-arrow" />
            </>
          )}
        </span>
      </button>

      <p ref={liveRef} className="sr-only" role="status" aria-live="polite" />
    </form>
  );
}
