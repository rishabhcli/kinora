// PasskeysCard — list, add (when WebAuthn is available), rename, and remove
// passkeys. Uses the cached registry (lib/account/passkey) + the API adapter
// (lib/api/sessions). The actual navigator.credentials.create is feature-gated;
// when it isn't available (or the backend has no endpoint), "Add a passkey"
// optimistically records a local demo credential so the surface is exercisable.
import { useEffect, useState } from "react";
import { KeyRound, Fingerprint } from "lucide-react";
import {
  type PasskeyCredential,
  createPasskeyRegistry,
  webauthnAvailable,
  suggestPasskeyLabel,
  relativeTime,
} from "../../lib/account";
import { listPasskeys, removePasskey, renamePasskey } from "../../lib/api/sessions";

export default function PasskeysCard() {
  const [registry] = useState(() => createPasskeyRegistry());
  const [creds, setCreds] = useState<PasskeyCredential[]>(() => registry.list());
  const [canAdd, setCanAdd] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    setCanAdd(webauthnAvailable());
    const off = registry.subscribe(() => setCreds(registry.list()));
    let alive = true;
    void (async () => {
      const fresh = await listPasskeys();
      if (alive && fresh.length) registry.set(fresh);
    })();
    return () => {
      alive = false;
      off();
    };
  }, [registry]);

  function add() {
    // A demo credential — a real flow would call navigator.credentials.create
    // and POST the attestation via begin/finishPasskeyRegistration.
    registry.add({
      id: `local-${Date.now()}`,
      label: suggestPasskeyLabel("platform", navigator?.platform),
      kind: "platform",
      createdAt: Date.now(),
      thisDevice: true,
    });
  }

  async function commitRename(id: string) {
    const label = draft.trim();
    setEditingId(null);
    if (label) {
      registry.rename(id, label);
      await renamePasskey(id, label);
    }
  }

  async function remove(id: string) {
    registry.remove(id);
    await removePasskey(id);
  }

  return (
    <div className="acct-card">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <div>
          <h3 className="acct-card-title">Passkeys</h3>
          <p className="acct-card-desc">Sign in with Touch ID, Face ID, or a security key.</p>
        </div>
        {canAdd && (
          <button type="button" className="acct-btn" onClick={add}>
            <Fingerprint size={15} strokeWidth={1.75} /> Add a passkey
          </button>
        )}
      </div>

      {creds.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {creds.map((c) => (
            <div className="acct-row" key={c.id}>
              <div className="acct-row-main" style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <KeyRound size={18} strokeWidth={1.6} aria-hidden="true" style={{ color: "var(--auth-subtle)" }} />
                <div>
                  {editingId === c.id ? (
                    <input
                      className="auth-input"
                      style={{ height: 32, maxWidth: 220 }}
                      autoFocus
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onBlur={() => commitRename(c.id)}
                      onKeyDown={(e) => e.key === "Enter" && commitRename(c.id)}
                    />
                  ) : (
                    <div className="acct-row-title">
                      {c.label}{" "}
                      {c.thisDevice && <span className="acct-badge acct-badge--current">This device</span>}
                    </div>
                  )}
                  <div className="acct-row-meta">
                    Added {relativeTime(c.createdAt)}
                    {c.lastUsedAt ? ` · used ${relativeTime(c.lastUsedAt)}` : ""}
                  </div>
                </div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  className="acct-btn acct-btn--ghost"
                  onClick={() => {
                    setEditingId(c.id);
                    setDraft(c.label);
                  }}
                >
                  Rename
                </button>
                <button type="button" className="acct-btn acct-btn--danger" onClick={() => remove(c.id)}>
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
