// SecuritySection — password change, two-factor (TOTP) enrollment, and passkeys.
// Password change uses the lib/account/password policy + the API adapter; MFA
// hands off to MfaEnrollDialog; passkeys live in PasskeysCard.
import { useState } from "react";
import { Section } from "./primitives";
import PasswordField from "../auth/PasswordField";
import { assessPassword, validatePasswordChange } from "../../lib/account";
import { changePassword } from "../../lib/api/account";
import { ApiError } from "../../lib/api";
import MfaEnrollDialog from "./MfaEnrollDialog";
import PasskeysCard from "./PasskeysCard";
import RecentActivityCard from "./RecentActivityCard";
import DangerZone from "./DangerZone";

interface Props {
  email: string;
  /** Whether MFA is currently enabled (from the profile/security fetch). */
  mfaEnabled?: boolean;
  /** Called once account deletion is scheduled (host signs out / routes away). */
  onAccountDeleted?: () => void;
}

export default function SecuritySection({ email, mfaEnabled = false, onAccountDeleted }: Props) {
  const [enabled, setEnabled] = useState(mfaEnabled);
  const [enrolling, setEnrolling] = useState(false);

  return (
    <Section title="Security" sub="Passwords, two-factor authentication, and passkeys.">
      <PasswordCard email={email} />

      <div className="acct-card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <div>
            <h3 className="acct-card-title">Two-factor authentication</h3>
            <p className="acct-card-desc">
              {enabled ? "On — codes from your authenticator are required." : "Add a second factor at sign-in."}
            </p>
          </div>
          {enabled ? (
            <span className="acct-badge acct-badge--good">Enabled</span>
          ) : (
            <button type="button" className="acct-btn acct-btn--primary" onClick={() => setEnrolling(true)}>
              Set up
            </button>
          )}
        </div>
        {enrolling && (
          <div style={{ marginTop: 14 }}>
            <MfaEnrollDialog
              account={email}
              onCancel={() => setEnrolling(false)}
              onDone={() => {
                setEnrolling(false);
                setEnabled(true);
              }}
            />
          </div>
        )}
      </div>

      <PasskeysCard />

      <RecentActivityCard />

      <DangerZone email={email} onDeleted={onAccountDeleted} />
    </Section>
  );
}

function PasswordCard({ email }: { email: string }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const assessment = assessPassword(next);
  const localError = next ? validatePasswordChange(current, next, confirm) : null;

  async function submit() {
    const err = validatePasswordChange(current, next, confirm);
    if (err) {
      setMsg({ kind: "err", text: err });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await changePassword({ current_password: current, new_password: next });
      setMsg({ kind: "ok", text: "Password updated." });
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (e) {
      setMsg({
        kind: "err",
        text: e instanceof ApiError && e.status === 401 ? "Your current password is incorrect." : "Couldn't update your password.",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="acct-card">
      <h3 className="acct-card-title">Password</h3>
      <p className="acct-card-desc" style={{ marginBottom: 12 }}>
        For <strong>{email}</strong>.
      </p>
      <PasswordField id="cur-pw" label="Current password" value={current} onChange={setCurrent} autoComplete="current-password" />
      <PasswordField id="new-pw" label="New password" value={next} onChange={setNext} autoComplete="new-password" meter />
      <PasswordField id="conf-pw" label="Confirm new password" value={confirm} onChange={setConfirm} autoComplete="new-password" />

      {assessment.warning && next && (
        <p className="acct-card-desc" style={{ color: "var(--auth-gold-bright)" }}>
          {assessment.warning}
        </p>
      )}
      {msg && (
        <p
          className={`auth-formmsg ${msg.kind === "ok" ? "auth-formmsg--info" : "auth-formmsg--error"}`}
          role={msg.kind === "ok" ? "status" : "alert"}
        >
          {msg.text}
        </p>
      )}

      <button
        type="button"
        className="acct-btn acct-btn--primary"
        style={{ marginTop: 10 }}
        disabled={busy || Boolean(localError) || !current || !next}
        onClick={submit}
      >
        {busy ? "Updating…" : "Update password"}
      </button>
    </div>
  );
}
