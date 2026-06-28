// PasskeyButton — "Sign in with a passkey". Feature-detects WebAuthn and hides
// itself when unavailable (older Electron, no platform authenticator). The
// actual navigator.credentials.get is performed by the caller's handler; this
// component only renders the affordance + capability gate.
import { useEffect, useState } from "react";
import { Fingerprint } from "lucide-react";
import { webauthnAvailable } from "../../lib/account";

interface Props {
  /** Invoked when the user opts to use a passkey. */
  onUse: () => void;
  disabled?: boolean;
  label?: string;
}

export default function PasskeyButton({ onUse, disabled, label = "Use a passkey" }: Props) {
  const [available, setAvailable] = useState(false);

  useEffect(() => {
    setAvailable(webauthnAvailable());
  }, []);

  if (!available) return null;

  return (
    <button type="button" className="acct-passkey-btn" disabled={disabled} onClick={onUse}>
      <Fingerprint size={17} strokeWidth={1.75} aria-hidden="true" />
      <span>{label}</span>
    </button>
  );
}
