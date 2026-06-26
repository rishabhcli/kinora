// A labelled auth input: visible label (never placeholder-only), leading glyph,
// optional trailing slot, and an error line that's reserved in layout (no jump)
// and wired with aria-invalid + aria-describedby. The form's central aria-live
// region announces the summary on submit; per-field errors are described-by.
import { forwardRef, type InputHTMLAttributes, type ReactNode } from "react";
import AuthIcon, { type AuthIconName } from "./AuthIcon";

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  id: string;
  label: string;
  icon?: AuthIconName;
  error?: string | null;
  /** show the error text (true once the field has been touched / submitted) */
  showError?: boolean;
  trailing?: ReactNode;
}

const Field = forwardRef<HTMLInputElement, FieldProps>(function Field(
  { id, label, icon, error, showError, trailing, className, "aria-describedby": describedBy, ...input },
  ref,
) {
  const invalid = Boolean(showError && error);
  const errorId = `${id}-error`;
  // Merge the caller's aria-describedby (e.g. the strength meter) with the error
  // id so neither clobbers the other.
  const describe = [invalid ? errorId : null, describedBy].filter(Boolean).join(" ") || undefined;
  return (
    <div className="auth-field">
      <label htmlFor={id} className="auth-field-label">
        {label}
      </label>
      <div className={`auth-input-wrap${invalid ? " is-invalid" : ""}`}>
        {icon && (
          <span className="auth-input-icon" aria-hidden="true">
            <AuthIcon name={icon} size={17} brand={false} />
          </span>
        )}
        <input
          ref={ref}
          id={id}
          className={`auth-input${icon ? " has-icon" : ""}${trailing ? " has-trailing" : ""}${className ? ` ${className}` : ""}`}
          aria-invalid={invalid || undefined}
          aria-describedby={describe}
          {...input}
        />
        {trailing && <span className="auth-input-trailing">{trailing}</span>}
      </div>
      <p id={errorId} className="auth-field-error" aria-hidden={!invalid}>
        {invalid ? error : " "}
      </p>
    </div>
  );
});

export default Field;
