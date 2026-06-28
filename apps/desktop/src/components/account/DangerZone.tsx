// DangerZone — data export + account deletion. Deletion is double-gated: the
// user must type their exact email to enable the confirm (no accidental
// deletes). Both flows route through the graceful account adapter.
import { useState } from "react";
import { Download, Trash2 } from "lucide-react";
import { deleteAccount, requestDataExport } from "../../lib/api/account";

interface Props {
  email: string;
  /** Called once deletion is scheduled (so the host can sign out / route away). */
  onDeleted?: () => void;
}

export default function DangerZone({ email, onDeleted }: Props) {
  const [exporting, setExporting] = useState(false);
  const [exported, setExported] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [scheduled, setScheduled] = useState<number | null>(null);

  const canDelete = typed.trim().toLowerCase() === email.trim().toLowerCase();

  async function exportData() {
    setExporting(true);
    const res = await requestDataExport();
    setExporting(false);
    setExported(res.url ?? (res.jobId ? "queued" : "queued"));
  }

  async function confirmDelete() {
    if (!canDelete) return;
    setDeleting(true);
    const { deletedAt } = await deleteAccount(email);
    setDeleting(false);
    setScheduled(deletedAt);
    onDeleted?.();
  }

  return (
    <div className="acct-card" style={{ borderColor: "rgba(232,140,106,0.3)" }}>
      <h3 className="acct-card-title">Your data</h3>

      <div className="acct-row">
        <div className="acct-row-main">
          <div className="acct-row-title">Export your data</div>
          <div className="acct-row-meta">A copy of your library, notes, and account.</div>
        </div>
        <button type="button" className="acct-btn" disabled={exporting} onClick={exportData}>
          <Download size={15} strokeWidth={1.75} /> {exporting ? "Preparing…" : "Export"}
        </button>
      </div>
      {exported && (
        <p className="auth-formmsg auth-formmsg--info" role="status">
          {exported === "queued"
            ? "We're preparing your export — you'll get an email when it's ready."
            : "Your export is ready to download."}
        </p>
      )}

      <div className="acct-row" style={{ paddingBottom: 0 }}>
        <div className="acct-row-main">
          <div className="acct-row-title" style={{ color: "var(--auth-danger)" }}>
            Delete account
          </div>
          <div className="acct-row-meta">Permanently remove your account and all data.</div>
        </div>
        {!confirmOpen && (
          <button type="button" className="acct-btn acct-btn--danger" onClick={() => setConfirmOpen(true)}>
            <Trash2 size={15} strokeWidth={1.75} /> Delete
          </button>
        )}
      </div>

      {confirmOpen && scheduled === null && (
        <div style={{ marginTop: 12 }}>
          <p className="acct-card-desc" style={{ marginBottom: 8 }}>
            Type <strong>{email}</strong> to confirm. This can't be undone.
          </p>
          <input
            className="auth-input"
            style={{ height: 40, marginBottom: 10 }}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={email}
            aria-label="Confirm your email to delete"
          />
          <div style={{ display: "flex", gap: 10 }}>
            <button
              type="button"
              className="acct-btn acct-btn--danger"
              disabled={!canDelete || deleting}
              onClick={confirmDelete}
            >
              {deleting ? "Deleting…" : "Delete my account"}
            </button>
            <button type="button" className="acct-btn acct-btn--ghost" onClick={() => setConfirmOpen(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {scheduled !== null && (
        <p className="auth-formmsg auth-formmsg--info" role="status" style={{ marginTop: 12 }}>
          Your account is scheduled for deletion on {new Date(scheduled).toLocaleDateString()}.
        </p>
      )}
    </div>
  );
}
