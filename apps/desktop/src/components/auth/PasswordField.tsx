// Password field: builds on Field with a real show/hide toggle button
// (aria-pressed + label) and, in register mode, a strength meter derived from the
// pure passwordStrength() estimator.
import { forwardRef, useId, useState } from "react";
import Field from "./Field";
import AuthIcon from "./AuthIcon";
import { passwordStrength } from "./validation";

interface Props {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  onBlur?: () => void;
  error?: string | null;
  showError?: boolean;
  autoComplete?: string;
  /** show the strength meter (sign-up only) */
  meter?: boolean;
}

const PasswordField = forwardRef<HTMLInputElement, Props>(function PasswordField(
  { id, label, value, onChange, onBlur, error, showError, autoComplete, meter = false },
  ref,
) {
  const [visible, setVisible] = useState(false);
  const meterId = useId();
  const strength = meter ? passwordStrength(value) : null;

  return (
    <div className="auth-password">
      <Field
        ref={ref}
        id={id}
        label={label}
        icon="lock"
        type={visible ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
        error={error}
        showError={showError}
        autoComplete={autoComplete}
        aria-describedby={strength && value ? meterId : undefined}
        trailing={
          <button
            type="button"
            className="auth-eye"
            aria-pressed={visible}
            aria-label={visible ? "Hide password" : "Show password"}
            onClick={() => setVisible((v) => !v)}
            tabIndex={0}
          >
            <AuthIcon name={visible ? "eye-off" : "eye"} size={17} brand={false} />
          </button>
        }
      />
      {strength && value && (
        <div className="auth-strength" id={meterId}>
          <div className="auth-strength-track" aria-hidden="true">
            <span className={`auth-strength-fill s${strength.score}`} />
          </div>
          <span className="auth-strength-label">{strength.label}</span>
        </div>
      )}
    </div>
  );
});

export default PasswordField;
