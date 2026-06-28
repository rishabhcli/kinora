// SignUpForm — registration. Same a11y contract as SignInForm, plus a real
// password policy (lib/account/password): a requirements checklist + the
// strength meter that PasswordField already shows in register mode, and a
// confirm-password field. Sign-up is gated on meetsPolicy + matching confirm.
import { useRef, useState, type FormEvent } from "react";
import Field from "./Field";
import PasswordField from "./PasswordField";
import AuthIcon from "./AuthIcon";
import { validateEmail } from "./validation";
import { passwordRequirements, meetsPolicy, isCommonPassword } from "../../lib/account";

export interface SignUpFormProps {
  onSubmit: (email: string, password: string) => void;
  busy?: boolean;
  error?: string | null;
  initialEmail?: string;
}

export default function SignUpForm({ onSubmit, busy, error, initialEmail = "" }: SignUpFormProps) {
  const [email, setEmail] = useState(initialEmail);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [touched, setTouched] = useState({ email: false, password: false, confirm: false });
  const liveRef = useRef<HTMLParagraphElement>(null);

  const emailErr = validateEmail(email);
  const reqs = passwordRequirements(password);
  const policyOk = meetsPolicy(password);
  const passErr = !password
    ? "Enter a password."
    : !policyOk
      ? "Use at least 8 characters with some variety."
      : isCommonPassword(password)
        ? "Choose something less common."
        : null;
  const confirmErr = confirm !== password ? "Passwords don't match." : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    setTouched({ email: true, password: true, confirm: true });
    const firstErr = emailErr ?? passErr ?? confirmErr;
    if (firstErr) {
      if (liveRef.current) liveRef.current.textContent = firstErr;
      return;
    }
    onSubmit(email.trim(), password);
  }

  return (
    <form onSubmit={submit} className="auth-form" noValidate aria-busy={busy || undefined}>
      <Field
        id="signup-email"
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
        id="signup-password"
        label="Password"
        value={password}
        onChange={setPassword}
        onBlur={() => setTouched((t) => ({ ...t, password: true }))}
        error={passErr}
        showError={touched.password}
        autoComplete="new-password"
        meter
      />

      {/* requirements checklist — only once the field is touched + non-empty */}
      {touched.password && password && (
        <ul className="acct-pw-reqs" aria-label="Password requirements">
          {reqs.map((r) => (
            <li key={r.id} className={r.met ? "is-met" : ""}>
              <AuthIcon name={r.met ? "check" : "alert"} size={12} brand={false} />
              {r.label}
            </li>
          ))}
        </ul>
      )}

      <PasswordField
        id="signup-confirm"
        label="Confirm password"
        value={confirm}
        onChange={setConfirm}
        onBlur={() => setTouched((t) => ({ ...t, confirm: true }))}
        error={confirmErr}
        showError={touched.confirm && Boolean(confirm)}
        autoComplete="new-password"
      />

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
              Create account
              <AuthIcon name="arrow-right" size={16} className="auth-submit-arrow" />
            </>
          )}
        </span>
      </button>

      <p ref={liveRef} className="sr-only" role="status" aria-live="polite" />
    </form>
  );
}
